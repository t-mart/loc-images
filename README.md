# loc-images

Output a list of images from a Library of Congress query.

This program does not download the images. If downloading is desired, this program should be used
in combination with a tool such as [aria2](https://aria2.github.io/).

## Usage

```shell
loc-images "https://www.loc.gov/collections/andre-kostelanetz-collection/" --no-aria-format
```

```shell
https://tile.loc.gov/image-services/iiif/service:music:muskostelanetz:muskostelanetz-1000035:muskostelanetz-1000035.0001/full/pct:50.0/0/default.jpg#h=3950&w=2288
https://tile.loc.gov/image-services/iiif/service:music:muskostelanetz:muskostelanetz-1000359:muskostelanetz-1000359.0001/full/pct:50.0/0/default.jpg#h=4013&w=2393
https://tile.loc.gov/image-services/iiif/service:music:muskostelanetz:muskostelanetz-1000744:muskostelanetz-1000744.0001/full/pct:50.0/0/default.jpg#h=3942&w=2383
...
```

Or, with aria2:

```shell
loc-images "https://www.loc.gov/collections/andre-kostelanetz-collection/" | aria2 -i -
```

### Rate Limiting

The Library of Congress has
[rate limits to crawling its API](https://www.loc.gov/apis/json-and-yaml/). For collections
(like what this program searches), you can perform 80 requests per minute. Not following this will
lead to a 1 hour ban.

**This rate limit is built into the program. You do not have to do anything to follow it.**

### Retries

Frequently during testing, I encountered 500 status codes in responses. This may be the rate
limiting in action -- I am not sure.

Nonetheless, to be robust in case limits are encountered, especially because searches can be many
pages long, this program utilizes an exponential backoff retry policy on requests to the LoC API.

The minimum retry delay is consistent with the 80 requests per minute rate. The maximum delay is
4096 seconds, just over the 1 hour ban time in case one has been issued to you.

## Installation

Ensure you have Python >= 3.10.

```shell
git clone https://github.com/t-mart/loc-images.git
pip install --user loc-images/  # or pipx, or virtualenv, or whatever
```

## References

- <https://labs.loc.gov/lc-for-robots/>
- <https://github.com/LibraryOfCongress/data-exploration/blob/master/Accessing%20images%20for%20analysis.ipynb>
- <https://libraryofcongress.github.io/data-exploration/>
