"""
Output a list of image URLs from a Library of Congress collection.
"""

# TODO:
# - bump results per page?
# - include collection name in aria format (what if no collection name?)
# - instead of exclude original_format types, include only images?

import time
from pathlib import Path
from typing import Any, Optional

from glom import glom

import arrow
import click
import httpx
from rich.console import Console
from tenacity import (  # type: ignore
    RetryCallState,
    retry,
    retry_if_exception_type,
    wait_exponential,
)
from yarl import URL

# user-friendly wrapper around stdout, prints statuses nicely
CONSOLE = Console(stderr=True)

# original format types that we don't want to download
SKIP_ORIGINAL_FORMAT_TYPES = ["collection", "web page"]

# Set of characters blocked in filenames by nix/windows/osx.
# Source: https://stackoverflow.com/a/31976060/235992
BLOCKED_FILE_NAME_CHARS = {chr(i) for i in range(32)} | set(R'<>:"/\|?*')

# time to wait between requests (and the minimum retry delay)
# For us, I think it's this crawl limit:
# > "80 requests per 1 minute, Block for 1 hour"
# https://www.loc.gov/apis/json-and-yaml/
SECONDS_PER_REQUEST_LIMIT = 60 / 80

# max amount of time to wait between retries
# just a bit more than the ban length of 1 hour, and is a multiple of two (exp backoff)
MAX_WAIT_RETRY_DELAY = 4096

# max length of the path stem for aria2 formatting
# this is arbitrary, but i know there's a limit, and it's probably just a bit over this
MAX_PATH_STEM_LENGTH = 200

# max dir length. again, arbitrary, but i know there's some limit
MAX_DIR_NAME_LENGTH = 200


def print_failed_try(retry_state: RetryCallState) -> None:
    """Print some status information about retrying the method."""
    if retry_state.outcome is None:
        return  # technically optional, but it should always be present after fail
    exception = retry_state.outcome.exception(timeout=0)
    if exception is None:
        return  # have to check for this according to concurrent.futures docs.

    next_wait_seconds = retry_state.retry_object.wait(retry_state)
    next_wait_expiry = arrow.now().shift(seconds=+next_wait_seconds)

    CONSOLE.print(
        (
            f"\tRequest attempt [bold]#{retry_state.attempt_number}[/bold] threw "
            f"exception: [red]{exception.args[0]}[/red]. Retrying after wait of "
            f"{next_wait_seconds} seconds ({next_wait_expiry})..."
        )
    )


class RetryableHTTPException(Exception):
    """Retryable status in HTTP response."""


@retry(
    retry=retry_if_exception_type(RetryableHTTPException),
    after=print_failed_try,
    wait=wait_exponential(min=SECONDS_PER_REQUEST_LIMIT, max=MAX_WAIT_RETRY_DELAY),
)
def send_request(request: httpx.Request, client: httpx.Client) -> httpx.Response:
    """
    Return the response of the HTTP request object.

    Timeouts, status 429, and 500s statuses will throw `RetryableHTTPException`.
    """
    # in testing, the LOC api has been spewing 500s. maybe this is the rate limiting?
    try:
        response = client.send(request)
    except httpx.ReadTimeout as read_timeout:
        raise RetryableHTTPException("Read time out") from read_timeout
    if response.status_code != httpx.codes.OK:
        if response.status_code == 429 or response.status_code in range(500, 600):
            # i haven't seen a 429, but i'd imagine that's what they'd use. for now,
            # i've just seen 500s, so i'm not sure what to think.
            raise RetryableHTTPException(f"Status code == {response.status_code}")
        raise click.ClickException(f"Non-retryable status code {response.status_code}")
    return response


def file_name_sanitize(name: str) -> str:
    """
    Return a file name that should be suitable on most OSes. Specifically, certain known
    bad characters will be filtered out.
    """
    return "".join(c for c in name if c not in BLOCKED_FILE_NAME_CHARS)


def create_filename(result: dict[str, Any], image_url: str) -> str:
    """
    Create an out filename for aria2. Tries to ensure its not too long, nor contains
    illegal characters.
    """
    id_number = Path(URL(result["url"]).path).parts[-1]
    title = file_name_sanitize(result["title"])
    stem = f"{id_number} - {title}"[:MAX_PATH_STEM_LENGTH]

    # this suffix determination is a little brittle.
    # it'd be better to make an http HEAD request to the url and inspect
    # the Content-Type header, but that's a lot more requests.
    # this works for now because the URLs contain the suffixes: https://foo.com/lol.jpg
    suffix = Path(URL(image_url).path).suffix

    safe_title = f"{stem}{suffix}"

    return safe_title


def create_collection_dir_path(result: dict[str, Any], root_dir: Path) -> Path:
    """
    Create a dir name for aria2, which will be somewhere under root_dir. Tries to ensure
    its not too long, nor contains illegal characters. This dir name is based on the
    "source_collection" key of the result json, such as "<root_dir>/<source_collection>.
    If this key does not exist, the returned Path is simply the root_dir itself.
    """
    if (
        source_collection := glom(result, "item.source_collection", default=None)
    ) is not None:
        return root_dir / file_name_sanitize(source_collection)
    return root_dir


def get_highest_quality_image_url(result: dict[str, Any]) -> Optional[str]:
    """
    Returns the highest quality image URL from the result dict. If one does not exist,
    returns None.
    """
    image_urls = result["image_url"]

    if not image_urls:
        return None

    # according to
    # https://github.com/LibraryOfCongress/data-exploration/blob/master/Accessing%20images%20for%20analysis.ipynb
    # the last image_url is the highest quality?

    # another note: going to the result url itself (under result['id']) yields
    # often the highest resolution version. But, they're usually TIF files that are
    # like 100MB a pop. Too much. I'm just using this program for browsing or setting
    # my wallpaper. I don't need archival quality.
    best_image: str = result["image_url"][-1]
    return best_image


def create_aria_option_line(key: str, value: str) -> str:
    return f"  {key}={value}"


class AriaDirPathParamType(click.ParamType):
    """
    Click parameter type for directory Path objects that can be fed to aria2c's --dir
    option.

    aria2c will fail if the path exists but is not a directory, so the converter of this
    class does the same. If the path exists and is a directory, aria2c will use it
    as-is. And finally, if the path does not exist, aria2c will create a directory at
    it.
    """

    name = "path"

    def convert(
        self,
        value: Any,
        param: Optional[click.Parameter],
        ctx: Optional[click.core.Context],
    ) -> Path:
        """
        Return a Path object from a command line value. Fail if the value cannot be
        turned into a Path or if the path exists and is not a directory.
        """
        if isinstance(value, Path):
            path = value
        else:
            try:
                path = Path(value)
            except ValueError:
                self.fail(f"{value!r} does not represent a path", param, ctx)
        if path.exists() and not path.is_dir():
            self.fail(f"{value!r} exists, but is not a directory", param, ctx)
        return path


ARIA_DIR_PATH_PARAM_TYPE = AriaDirPathParamType()


@click.command()
@click.argument("url")
@click.option(
    "--aria-format/--no-aria-format",
    default=True,
    help=(
        "Outputs image URLs in a format that aria2c understands. (aria2c can consume "
        "this file with the -i/--input-file option.) For each url, the "
        '"out" option is set to the item\'s title and "auto-file-renaming" is '
        "disabled to prevent clobbering of preexisting files. See "
        "https://aria2.github.io/manual/en/html/aria2c.html#input-file for more info."
    ),
)
@click.option(
    "--group-by-collection/--no-group-by-collection",
    default=True,
    help=(
        'When aria2c formatting, set the "dir" option to the item\'s collection name. '
        "Items without a collection will be downloaded directly to the path of "
        "root-dir."
    ),
)
@click.option(
    "--root-dir",
    default=Path("."),
    type=ARIA_DIR_PATH_PARAM_TYPE,
    help=("When aria2c formatting, set the root directory of image downloads."),
)
def main(
    url: str, aria_format: bool, group_by_collection: bool, root_dir: Path
) -> None:
    """
    Output a list of images from a Library of Congress query at URL.

    For example:

    - loc-images "https://www.loc.gov/collections/baseball-cards/"

    - loc-images "https://www.loc.gov/photos/?q=bridges&dates=1800%2F1899"
    """
    cur_url = url

    with httpx.Client() as client:
        while True:
            CONSOLE.print(f"Getting images from [link={cur_url}]{cur_url}[/link]")

            request = httpx.Request(
                method="GET",
                url=cur_url,
                params={"fo": "json", "c": 100, "at": "results,pagination"},
            )

            response = send_request(request, client)
            data = response.json()

            import json

            with (Path(__file__).parent / "last.json").open("w") as f:
                json.dump(data, f, indent=2)

            for result in data["results"]:
                if any(
                    t in result["original_format"] for t in SKIP_ORIGINAL_FORMAT_TYPES
                ):
                    continue

                image_url = get_highest_quality_image_url(result)

                if image_url is None:
                    continue

                lines = [image_url]

                if aria_format:
                    # little comment for humans about what the source file is
                    # put at beginning
                    lines.insert(0, f"# {result['id']}")

                    # sets the name of the output file. the defaults are gross
                    lines.append(
                        create_aria_option_line(
                            "out", create_filename(result, image_url)
                        )
                    )

                    # if you want a nicer grouped structure, this option has URLs
                    # downloaded to directories named by the collection under the root
                    # dir
                    if group_by_collection:
                        lines.append(
                            create_aria_option_line(
                                "dir", str(create_collection_dir_path(result, root_dir))
                            )
                        )

                    # forbids aria2 from downloading foo.1.jpg if foo.jpg exists, and
                    # instead, just skips the URL. we don't want duplicates, nor do we
                    # want to overwrite existing files.
                    lines.append(create_aria_option_line("auto-file-renaming", "false"))

                    # make it easier to read each url and its options with a line break
                    lines.append("")

                print("\n".join(lines))

            if data["pagination"]["next"] is not None:
                cur_url = data["pagination"]["next"]
                time.sleep(SECONDS_PER_REQUEST_LIMIT)
            else:
                break


if __name__ == "__main__":
    main.main()
