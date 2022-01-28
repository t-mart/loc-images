"""
Output a list of image URLs from a Library of Congress collection.
"""

import sys
from pathlib import Path
from typing import Any, Optional

import click
import httpx
from yarl import URL

SKIP_ORIGINAL_FORMAT_TYPES = ["collection", "web page"]

# Set of characters blocked in filenames by nix/windows/osx.
# Source: https://stackoverflow.com/a/31976060/235992
BLOCKED_FILE_NAME_CHARS = {chr(i) for i in range(32)} | set(R'<>:"/\|?*')


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
            response = client.get(
                cur_url, params={"fo": "json", "c": 100, "at": "results,pagination"}
            )
            if response.status_code != httpx.codes.OK:
                raise click.ClickException(
                    f"Got response status code {response.status_code} when requesting "
                    f"{cur_url}"
                )
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

                print("\n".join(lines))

            if data["pagination"]["next"] is not None:
                cur_url = data["pagination"]["next"]
            else:
                break


if __name__ == "__main__":
    main.main()
