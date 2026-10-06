import math

import pytest

from kalshi_bot.btc import (MAX_SIGMA_1M, MIN_SIGMA_1M, make_call, probability_above,
                            realized_sigma_1m)


def test_strike_at_spot_is_coin_flip():
    assert probability_above(80000, 80000, 8, 0.001) == pytest.approx(0.5)


def test_far_strike_is_near_certain():
    assert probability_above(80872, 85501, 8, 0.0011) < 0.001
    assert probability_above(80872, 76000, 8, 0.0011) > 0.999


def test_more_time_means_less_certain():
    near = probability_above(80000, 80300, 5, 0.001)
    far = probability_above(80000, 80300, 60, 0.001)
    assert near < far < 0.5


def test_realized_sigma_clamped_and_defaulted():
    assert realized_sigma_1m([1.0, 1.0]) == 0.0012             # too few -> default
    flat = [80000.0] * 100
    assert realized_sigma_1m(flat) == MIN_SIGMA_1M
    wild = [80000 * (1.5 ** (i % 2)) for i in range(100)]
    assert realized_sigma_1m(wild) == MAX_SIGMA_1M


def test_make_call_renders_verdict():
    closes = [80000 * math.exp(0.001 * ((-1) ** i)) for i in range(200)]
    call = make_call(spot=80872.9, strike=85203, minutes=8, closes=closes)
    assert call.verdict == "BELOW" and call.confidence > 0.99
    yes, no = call.fair_cents()
    assert yes == 1 and no == 99
    text = call.render()
    assert "BEST GUESS: BELOW" in text and "near certain" in text


def test_make_call_coin_flip_says_pass():
    closes = [80000 * math.exp(0.001 * ((-1) ** i)) for i in range(200)]
    call = make_call(spot=80000, strike=80010, minutes=8, closes=closes)
    assert "Pass" in call.render()


def test_invalid_inputs():
    with pytest.raises(ValueError):
        probability_above(0, 1, 1, 0.001)
    with pytest.raises(ValueError):
        probability_above(1, 1, 0, 0.001)
