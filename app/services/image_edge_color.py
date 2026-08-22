"""Sample the dominant colour of the strips a `contain` fit would letterbox.

The venue list card shows an Instagram profile avatar. Avatars are **square**
and the card's media box is wider than it is tall, so `cover` centre-crops
them — and measured on real production images they have essentially no safe
margin to trim (0.0%, 0.0%, 1.6%, 23.1% on the four sampled venues; Instagram
renders avatars as circles, so artwork runs to the edge by design). The app
therefore fits the avatar whole with `contain`, which leaves a strip of empty
box on each side.

Painting that strip in the avatar's own edge colour is what stops it reading as
a grey bar. React Native cannot sample pixels from a remote image, so the colour
has to be produced here, once, at archive time, and carried to the app as a
field.

## Which strips

A square source in a box wider than it is tall letterboxes on the **left and
right**. That is the only geometry fact this module encodes — deliberately not
the box's exact ratio, which belongs to the app and has already changed once
during this feature's design (101x96 -> 95x84). Both of those boxes are wider
than they are tall, which is the stable property.

## Modal-then-mean, not a plain average

A plain average of the border pixels muddies every result: JPEG ringing and a
few anti-aliased pixels from the artwork pull a pure white border to
`#FEFDFF`, and `#FEFDFF` next to a genuinely white avatar is a visible seam.
Instead the pixels are bucketed coarsely (each channel to the nearest 8), the
**modal** bucket wins, and the returned colour is the mean of only the pixels
inside it. A flat border therefore returns its exact hex, and a noisy one
returns its dominant colour rather than a blend of it with the artwork.

## Never raises

Every failure — an undecodable body, a zero-dimension image, an exotic mode —
returns `None`. A missing colour is an ABSENCE, not an error: the photo is
still stored, still projected, still served, and the app falls back to its own
neutral. Refusing to store a paid-for photo because its colour could not be
read would be strictly worse.
"""
from __future__ import annotations

import io
import logging
from collections import Counter
from typing import Optional

logger = logging.getLogger(__name__)

# Fraction of the image width sampled from each vertical edge. One column is
# enough in principle, but a single anti-aliased column of artwork bleeding
# into the frame would then decide the whole colour.
EDGE_STRIP_FRACTION = 0.02

# Channel quantisation step for the modal bucket. 8 is coarse enough to
# collapse codec noise into one bucket and fine enough that two genuinely
# different border colours never share one.
QUANTISATION_STEP = 8


def _quantise(channel: int) -> int:
    return min(255, int(round(channel / QUANTISATION_STEP)) * QUANTISATION_STEP)


def sample_edge_color(data: bytes) -> Optional[str]:
    """Return the dominant edge colour as an uppercase ``#RRGGBB``, or ``None``.

    Deterministic: identical bytes always produce an identical string.
    """
    if not data:
        return None
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as img:
            img.seek(0)
            width, height = img.size
            if width <= 0 or height <= 0:
                return None
            # Composite over WHITE rather than converting straight to RGB.
            # Instagram avatars are shown as circles on a light chrome, so a
            # transparent corner must resolve to white; a bare `convert("RGB")`
            # would hand back whatever undefined colour sits under alpha 0.
            rgba = img.convert("RGBA")
            flat = Image.new("RGB", (width, height), (255, 255, 255))
            flat.paste(rgba, mask=rgba.split()[3])

            strip = max(1, int(round(width * EDGE_STRIP_FRACTION)))
            strip = min(strip, width)
            pixels = list(flat.crop((0, 0, strip, height)).getdata())
            if strip < width:
                pixels += list(
                    flat.crop((width - strip, 0, width, height)).getdata()
                )
    except Exception as e:  # any decoder failure is an absence, never an error
        logger.info(f"[EdgeColor] could not sample image ({type(e).__name__}: {e})")
        return None

    if not pixels:
        return None

    buckets = Counter(
        (_quantise(r), _quantise(g), _quantise(b)) for r, g, b in pixels
    )
    winner, _ = buckets.most_common(1)[0]
    members = [
        p for p in pixels
        if (_quantise(p[0]), _quantise(p[1]), _quantise(p[2])) == winner
    ]
    count = len(members)
    mean = tuple(
        min(255, max(0, int(round(sum(p[i] for p in members) / count))))
        for i in range(3)
    )
    return "#{:02X}{:02X}{:02X}".format(*mean)
