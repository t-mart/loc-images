"""
Output a list of image URLs from a Library of Congress collection.
"""

import sys
import time
from pathlib import Path
from typing import Any, Optional

import click
import httpx
from tenacity import (  # type: ignore
    RetryCallState,
    retry,
    retry_if_exception_type,
    wait_exponential,
)
from yarl import URL

SKIP_ORIGINAL_FORMAT_TYPES = ["collection", "web page"]

# Set of characters blocked in filenames by nix/windows/osx.
# Source: https://stackoverflow.com/a/31976060/235992
BLOCKED_FILE_NAME_CHARS = {chr(i) for i in range(32)} | set(R'<>:"/\|?*')

# For us, I think it's the crawl limit:
#   Collections, format, and other endpoints:
#   - Burst Limit  	20 requests per 10 seconds, Block for 5 minutes
#   - Crawl Limit 	80 requests per 1 minute, Block for 1 hour
# https://www.loc.gov/apis/json-and-yaml/
SECONDS_PER_REQUEST_LIMIT = 60 / 80


class RetryableStatusException(Exception):
    """Retryable status in HTTP response."""


def print_retrying(retry_state: RetryCallState) -> None:
    """Print the retry attempt."""
    if retry_state.attempt_number > 1:
        print(
            f"\tRetrying HTTP request, attempt #{retry_state.attempt_number}",
            file=sys.stderr,
        )


@retry(
    retry=retry_if_exception_type(RetryableStatusException),
    before=print_retrying,
    wait=wait_exponential(multiplier=1, max=4096),
)
def http_get_query(url: str, client: httpx.Client) -> httpx.Response:
    """Return the response of an HTTP GET request."""
    # in testing, the LOC api has been spewing 500s. maybe this is rate limiting?
    response = client.get(
        url, params={"fo": "json", "c": 100, "at": "results,pagination"}
    )
    if response.status_code != httpx.codes.OK:
        if response.status_code == 429 or response.status_code in range(500, 600):
            # i haven't seen a 429, but i'd imagine that's what they'd use.
            raise RetryableStatusException(
                f"Got response status code {response.status_code} when requesting "
                f"{url}, but this seems to just be rate-limiting"
            )
        raise click.ClickException(
            f"Got response status code {response.status_code} when requesting " f"{url}"
        )
    return response


def file_name_sanitize(name: str) -> str:
    """
    Return a file name that should be suitable on most OSes. Specifically, certain known
    bad characters will be filtered out.
    """
    return "".join(c for c in name if c not in BLOCKED_FILE_NAME_CHARS)


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


@click.command()
@click.argument("url")
@click.option(
    "--aria-format/--no-aria-format",
    default=True,
    help=(
        "Additionally outputs a more descriptive file title of the images in a format "
        "that aria2c understands."
    ),
)
def main(url: str, aria_format: bool) -> None:
    """
    Output a list of images from a Library of Congress query at URL.

    For example:

    - loc-images "https://www.loc.gov/collections/baseball-cards/"

    - loc-images "https://www.loc.gov/photos/?q=bridges&dates=1800%2F1899"
    """
    cur_url = url

    with httpx.Client() as client:
        while True:
            print(f"Getting images from {cur_url}", file=sys.stderr)
            response = http_get_query(cur_url, client)
            data = response.json()
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
                    lines.insert(0, f"# {result['id']}")

                    safe_title = file_name_sanitize(result["title"])
                    # this suffix determination is a little brittle.
                    # it'd be better to make an http HEAD request to the url and inspect
                    # the Content-Type header
                    suffix = Path(URL(image_url).path).suffix
                    lines.append(f"  out={safe_title}{suffix}")
                    lines.append("  auto-file-renaming=false")

                print("\n".join(lines))

            if data["pagination"]["next"] is not None:
                cur_url = data["pagination"]["next"]
                time.sleep(SECONDS_PER_REQUEST_LIMIT)
            else:
                break


if __name__ == "__main__":
    main.main()
