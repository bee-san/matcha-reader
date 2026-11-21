# Light sleep with the frontlight on

On the X4 Pro the main loop light-sleeps between page turns (`canIdleLightSleep()` in
`src/main.cpp`). It keeps sleeping while the frontlight is on.

## How the light survives light sleep

The `x4pro` envs build with `-DFREEINK_FRONTLIGHT_LS=1`. The SDK then clocks the light's LEDC timer
from RC_FAST (about 17.5 MHz), which keeps running in light sleep, and marks both channels
`LEDC_SLEEP_MODE_KEEP_ALIVE` (`freeink-sdk/libs/hardware/FrontlightManager/src/FrontlightManager.cpp`).
RC_FAST is kept on in sleep only while the light is lit.

LEDC needs `frequency × 2^bits <= source clock`. The board profile asks for 25 kHz at 10 bits
(`{8, 25000, 10, true, 9}` in `BoardConfig.h`), which is 25.6 MHz. When the request does not fit,
`attachChannel()` keeps the 10 bits and lowers the frequency to the highest RC_FAST can drive
(the calibrated RC_FAST frequency / 1024, about 17 kHz). The boot log shows the frequency used:
`attach: 25000 Hz does not fit RC_FAST at 10 bits, using ... Hz`.

This change lives on the `matcha/frontlight-ls-fit` branch of `bee-san/freeink-sdk`, which the
`freeink-sdk` submodule points at. Every SDK bump means rebasing it onto the new upstream commit.

## Deep sleep

A KEEP_ALIVE pad is not isolated when the chip sleeps. Before deep sleep, `enterDeepSleep()` calls
`Frontlight.prepareForDeepSleep()`: it turns the light off (which also releases RC_FAST), then drives
GPIO8/9 to the off level and holds them. `FrontlightManager::begin()` releases the hold on the next
boot.

## Check on the device

- No whine from the boost driver at about 17 kHz, at low and high brightness, in a quiet room. If it
  whines, keep 25 kHz and use 9 bits instead (512 steps; the lowest level is then about twice as
  bright).
- No flicker or brightness step on each sleep and wake.
- The light is fully off in deep sleep (no faint glow), and it comes back after waking.
