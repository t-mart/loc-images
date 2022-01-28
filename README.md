# loc-images

Output a list of images from a Library of Congress collection.

This program does not download the images. (Use `aria2c -i <file>` for that, for
example.)


## Usage

```shell
loc-images "https://www.loc.gov/collections/andre-kostelanetz-collection/"
```

```shell
https://tile.loc.gov/image-services/iiif/service:music:muskostelanetz:muskostelanetz-1000035:muskostelanetz-1000035.0001/full/pct:50.0/0/default.jpg#h=3950&w=2288
https://tile.loc.gov/image-services/iiif/service:music:muskostelanetz:muskostelanetz-1000359:muskostelanetz-1000359.0001/full/pct:50.0/0/default.jpg#h=4013&w=2393
https://tile.loc.gov/image-services/iiif/service:music:muskostelanetz:muskostelanetz-1000744:muskostelanetz-1000744.0001/full/pct:50.0/0/default.jpg#h=3942&w=2383
...
```

## Installation

Ensure you have Python >= 3.10.

```shell
git clone https://github.com/t-mart/loc-images.git
pip install --user loc-images/  # or pipx, or virtualenv, or whatever
```

## References:

- <https://labs.loc.gov/lc-for-robots/>
- <https://github.com/LibraryOfCongress/data-exploration/blob/master/Accessing%20images%20for%20analysis.ipynb>
- <https://libraryofcongress.github.io/data-exploration/>