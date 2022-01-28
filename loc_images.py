"""
Output a list of images from a Library of Congress collection.
"""

import click
import httpx


@click.command()
@click.argument("url")
def main(url: str) -> None:
    """
    Output a list of images from a Library of Congress collections that resides at URL.

    For example:

    - loc-images "https://www.loc.gov/collections/baseball-cards/"

    - loc-images "https://www.loc.gov/photos/?q=bridges&dates=1800%2F1899"
    """
    cur_url = url
    while True:
        response = httpx.get(
            cur_url, params={"fo": "json", "c": 100, "at": "results,pagination"}
        )
        if response.status_code != httpx.codes.OK:
            raise click.ClickException(
                f"Got response status code {response.status_code} when requesting "
                f"{cur_url}"
            )
        data = response.json()
        results = data["results"]
        for result in results:
            # don't try to get images from the collection-level result
            if "collection" not in result.get(
                "original_format"
            ) and "web page" not in result.get("original_format"):
                # take the last URL listed in the image_url array
                if result.get("image_url"):
                    print(result.get("image_url")[-1])
        if data["pagination"]["next"] is not None:
            cur_url = data["pagination"]["next"]
        else:
            break


if __name__ == "__main__":
    main.main()
