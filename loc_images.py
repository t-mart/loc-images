"""
Output a list of image URLs from a Library of Congress collection.
"""

import time
from pathlib import Path
from typing import Any, Optional

import arrow
import click
import httpx
from rich.console import Console
from rich.padding import Padding
from tenacity import (  # type: ignore
    RetryCallState,
    retry,
    retry_if_exception_type,
    wait_exponential,
)
from yarl import URL

# user-friendly wrapper around stdout, prints statuses nicely
CONSOLE = Console(highlight=False, stderr=True)

# original formats (original representation of the item) that we want.
# other types are like 'manuscript/mixed material' or 'sound recording' or 'web site'.
# maybe in the future, we will support more types?
ORIGINAL_FORMAT_TYPES = {
    "photo, print, drawing",
    "map",
}

# online formats (LoC representation of the item) that we want
# again, maybe we can take more types in the future.
ONLINE_TYPES = {
    "image",
}

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

# results per page
# ideally, its as high as possible to reduce request count. but somewhere too high, the
# server will just reset it down to the lowest of 25. further, in testing, i've had the
# server close the connection because its taking too long (but we have logic to react to
# that if it happens.)
# NOTE: this should be a power of two! so that it can adaptively be split in half (many
# times) if we do encounter connection closure. See more about this in
# `get_loc_response_json`
STARTING_RESULTS_PER_PAGE = 512


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
        left_pad(
            f"Request attempt [bold]#{retry_state.attempt_number}[/bold] threw "
            f"exception: [red]{exception.args[0]}[/red]. Retrying after wait of "
            f"{next_wait_seconds} seconds ({next_wait_expiry})...",
            level=1,
        ),
    )


class RetryableHTTPException(Exception):
    """Retryable status in HTTP response."""


@retry(
    retry=retry_if_exception_type(RetryableHTTPException),
    after=print_failed_try,
    wait=wait_exponential(min=SECONDS_PER_REQUEST_LIMIT, max=MAX_WAIT_RETRY_DELAY),
)
def get_loc_response_json(url: httpx.URL, client: httpx.Client) -> dict[str, Any]:
    """
    Return the response of the HTTP request object.

    Timeouts, status 429, and 500s statuses will throw `RetryableHTTPException`.
    """
    # in testing, the LOC api has been spewing 500s. maybe this is the rate limiting?
    try:
        response = client.get(url=url)
        # response = httpx.Response(200)
        # raise httpx.RemoteProtocolError("lol")
    except httpx.ReadTimeout as read_timeout:
        raise RetryableHTTPException("Read time out") from read_timeout
    except httpx.RemoteProtocolError as remote_proto_error:
        # if we're here, it probably means LoC has closed the connection on their end
        # because it took too long.
        # so, let's readjust how much data we're asking for by reducing how many results
        # per page we get (query param "c"). thusly, we need to update the current page
        # we're on (query param "sp") to stay at the same spot.
        #
        # our reduction strategy is to split cur_results_per_page in half
        #
        # example:
        #   cur_results_per_page = 30, cur_page = 5
        #   decrease cur_results_per_page = 15
        #   then cur_page = 10 (✔)

        cur_results_per_page = int(client.params.get("c"))
        if cur_results_per_page % 2 != 0:
            # if we can't do split evenly because its odd, then we can't accurately
            # determine the new current page, so just fail.
            # example:
            #   cur_results_per_page = 15, cur_page = 10
            #   decrease cur_results_per_page = 7, maybe 8? doesn't cleanly divide
            #   then cur_page = ??? it depends
            #
            # this is why using a power of two is a good idea: you get a lot of splits
            raise remote_proto_error
        new_results_per_page = cur_results_per_page // 2
        # set the requests per page on the client! so it persists through out the rest
        # of the program lifetime.
        client.params = client.params.set("c", str(new_results_per_page))

        cur_page = int(url.params.get("sp", 1)) - 1  # first page is one
        new_page = cur_page * 2
        new_url = url.copy_set_param("sp", str(new_page + 1))

        CONSOLE.print(
            left_pad(
                f"Got {remote_proto_error}. Possibly due to requests per page being "
                f"too high. Decreasing from {cur_results_per_page} to "
                f"{new_results_per_page}.",
                level=1,
            ),
        )

        return get_loc_response_json(new_url, client)
    if response.status_code != httpx.codes.OK:
        if response.status_code == 429 or response.status_code in range(500, 600):
            # i haven't seen a 429, but i'd imagine that's what they'd use. for now,
            # i've just seen 500s, so i'm not sure what to think.
            raise RetryableHTTPException(f"Status code == {response.status_code}")
        raise click.ClickException(f"Non-retryable status code {response.status_code}")

    data: dict[str, Any] = response.json()
    return data


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


def create_collection_dir_path(title: str, root_dir: Path) -> Path:
    """
    Create a dir name for aria2, which will be somewhere under root_dir. Tries to ensure
    its not too long, nor contains illegal characters. This dir name is based on the
    "title" key of the data json, such as "<root_dir>/<title>.
    """
    return root_dir / file_name_sanitize(title)[:MAX_DIR_NAME_LENGTH]


def get_largest_image_url(result: dict[str, Any]) -> Optional[str]:
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
    """Return a string that specifies an aria2c option, such as key=value."""
    # the whitespace is important: needs to be whitespace-prefixed to differentiate from
    # other URLs
    return f"  {key}={value}"


def left_pad(text: str, level: int) -> Padding:
    """Return a rich Padding object with only padding on the left."""
    return Padding(f"- {text}", (0, 0, 0, level * 4))


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
        '"out" option is set to the item\'s title, the "dir" option is set to the '
        'title of the collection, and the "auto-file-renaming" option is set to '
        "false to prevent clobbering of preexisting files. See "
        "https://aria2.github.io/manual/en/html/aria2c.html#input-file for more info."
    ),
)
@click.option(
    "--root-dir",
    default=Path("."),
    type=ARIA_DIR_PATH_PARAM_TYPE,
    help=(
        "When aria2c formatting, set the root directory of image downloads to this "
        "path."
    ),
)
def main(url: str, aria_format: bool, root_dir: Path) -> None:
    """
    Output a list of images from a Library of Congress query at URL.

    For example:

    - loc-images "https://www.loc.gov/collections/baseball-cards/"

    - loc-images "https://www.loc.gov/photos/?q=bridges&dates=1800%2F1899"

    Note: If you get charmap decode errors or something like that, you may have to set
    PYTHONIOENCODING='utf-8' in your shell.
    """
    cur_url = url

    # "results" give use the items to iterate over
    # "pagination" tells us about subsequent pages
    # "title" gives us the title of the search (or collection)
    params = {
        "fo": "json",
        "c": str(STARTING_RESULTS_PER_PAGE),
        "at": "results,pagination,title",
    }

    current_page = 0
    total_pages = 1  # just default for now, will get actual later

    with httpx.Client(params=params) as client:
        while True:
            percent_done = current_page / total_pages
            CONSOLE.print(
                left_pad(
                    f"GET [link={cur_url}]{cur_url}[/link] ({percent_done:.2%} done)",
                    level=0,
                ), no_wrap=True
            )

            data = get_loc_response_json(httpx.URL(cur_url), client)

            results = data["results"]
            pagination = data["pagination"]
            title = data["title"]

            current_page = pagination["current"]
            total_pages = pagination["total"]

            for result in results:
                # this usually means its only available at the physical library, not
                # online. in these cases, an image might be available online, but it'll
                # be really small
                if result["access_restricted"] is True:
                    continue

                # ensure one of the allowed ORIGINAL_FORMAT_TYPES is in the item's
                # original_format list
                #
                # honestly, it's kinda curious that there can be mulitple original
                # format types (it's a list). feels like an item can only have 1 type
                if not set(result["original_format"]) & ORIGINAL_FORMAT_TYPES:
                    continue

                # same as above, but with online types
                # we check for key presence first, because its not always there, such
                # as for https://www.loc.gov/item/afc1981004.b54868/ Probably LoC bug.
                # Our image finding logic below is robust enough to know if there's
                # not actually an image, so for now, we just let it slide and let that
                # code make the final call.
                if (
                    "online_format" in result
                    and not set(result["online_format"]) & ONLINE_TYPES
                ):
                    continue

                image_url = get_largest_image_url(result)

                if image_url is None:
                    continue

                lines = [image_url]

                if aria_format:
                    # little comment for humans about where to find the item on loc.gov
                    lines.insert(0, f"# {result['url']}")

                    # sets the name of the output file because the defaults are gross
                    lines.append(
                        create_aria_option_line(
                            "out", create_filename(result, image_url)
                        )
                    )

                    # group images into a nicer structure
                    lines.append(
                        create_aria_option_line(
                            "dir",
                            str(create_collection_dir_path(title, root_dir)),
                        )
                    )

                    # forbids aria2 from downloading foo.1.jpg if foo.jpg exists, and
                    # instead, just skips the URL. we don't want duplicates, nor do we
                    # want to overwrite existing files.
                    lines.append(create_aria_option_line("auto-file-renaming", "false"))

                    # make it easier to read each url chunk
                    lines.append("")

                print("\n".join(lines))

            if pagination["next"] is not None:
                cur_url = pagination["next"]
                time.sleep(SECONDS_PER_REQUEST_LIMIT)
            else:
                break


if __name__ == "__main__":
    main.main()
