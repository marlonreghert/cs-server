"""Unit tests for app/services/image_edge_color.sample_edge_color.

The BDD proves the colour reaches the row, Redis and the app. These tests pin
the sampler's own contract at a level the scenarios cannot reach: exactness on a
flat border, resistance to noise, determinism, and the promise that it never
raises.
"""
import io

import pytest

from app.services.image_edge_color import sample_edge_color

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402


def _png(img: "Image.Image") -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _avatar(border, centre=(255, 0, 0), size=100, mode="RGB"):
    """A square avatar with a flat frame and a contrasting centre.

    The centre DOMINATES by area (~64%) on purpose: a sampler that read the
    whole image rather than only the letterboxed vertical strips would return
    the centre colour, so every expectation below would fail. Verified by
    sabotage — widening the crop to the full image turns these tests red.
    """
    img = Image.new(mode, (size, size), border)
    inset = max(1, size // 10)
    img.paste(Image.new(mode, (size - 2 * inset,) * 2, centre), (inset, inset))
    return img


def test_flat_border_returns_its_exact_hex():
    """A pure border must come back pure. A plain average of the edge pixels
    would return something like #FEFDFF, which is a visible seam next to a
    genuinely white avatar."""
    assert sample_edge_color(_png(_avatar((255, 255, 255)))) == "#FFFFFF"
    assert sample_edge_color(_png(_avatar((0, 74, 157)))) == "#004A9D"
    assert sample_edge_color(_png(_avatar((23, 23, 23)))) == "#171717"


def test_output_is_uppercase_six_digit_hex():
    result = sample_edge_color(_png(_avatar((49, 84, 165))))
    assert result == "#3154A5"
    assert result == result.upper()
    assert len(result) == 7 and result.startswith("#")


def test_is_deterministic_across_calls():
    data = _png(_avatar((0, 74, 157)))
    assert len({sample_edge_color(data) for _ in range(5)}) == 1


def test_samples_the_vertical_edges_not_the_whole_image():
    """The centre occupies ~64% of the area, so a whole-image sampler returns
    WHITE here. Only the strips a `contain` fit letterboxes — the vertical
    ones — may be read, and they are black."""
    img = _avatar((0, 0, 0), centre=(255, 255, 255), size=100)
    assert sample_edge_color(_png(img)) == "#000000"


def test_horizontal_bands_do_not_win_over_the_vertical_edges():
    """A wide top/bottom band is NOT what shows beside a square avatar in a
    wider box. Painting the strip with it would be wrong, so it must lose."""
    img = Image.new("RGB", (100, 100), (0, 74, 157))
    img.paste(Image.new("RGB", (100, 20), (255, 0, 0)), (0, 0))
    img.paste(Image.new("RGB", (100, 20), (255, 0, 0)), (0, 80))
    assert sample_edge_color(_png(img)) == "#004A9D"


def test_noisy_border_returns_the_dominant_colour_not_a_blend():
    """Modal-then-mean: a few stray pixels must not drag the result toward the
    midpoint the way an average would."""
    img = _avatar((0, 74, 157))
    for y in range(0, 100, 17):
        img.putpixel((0, y), (255, 255, 0))
        img.putpixel((99, y), (255, 255, 0))
    result = sample_edge_color(_png(img))
    assert result == "#004A9D"


def test_transparent_border_composites_over_white():
    """Instagram shows avatars as circles on light chrome. A bare convert("RGB")
    would hand back whatever undefined colour sits under alpha 0."""
    img = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    inset = 25
    img.paste(Image.new("RGBA", (50, 50), (255, 0, 0, 255)), (inset, inset))
    assert sample_edge_color(_png(img)) == "#FFFFFF"


def test_greyscale_and_palette_images_are_handled():
    assert sample_edge_color(_png(_avatar(200, centre=10, mode="L"))) == "#C8C8C8"
    assert sample_edge_color(_png(_avatar((255, 255, 255)).convert("P"))) == "#FFFFFF"


def test_a_one_pixel_wide_image_still_samples():
    assert sample_edge_color(_png(Image.new("RGB", (1, 4), (18, 52, 86)))) == "#123456"


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not an image at all",
        b"\xff\xd8\xff\xe0truncated-jpeg",
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 32,
    ],
)
def test_undecodable_bytes_return_none_without_raising(data):
    """A missing colour is an absence, never an error: the photo is stored
    either way, and refusing a paid-for scrape over an unreadable colour would
    be strictly worse."""
    assert sample_edge_color(data) is None
