# Light sleep with the frontlight on (idea, not done)

## Today

On the X4 Pro the main loop light-sleeps between page turns (`canIdleLightSleep()` in
`src/main.cpp`). It does not sleep while the frontlight is on. The light's LEDC PWM runs from
the APB clock, and that clock stops in light sleep, so the light would go dark. When you read
with the light on, the CPU stays awake at its idle clock the whole time.

## The SDK already has most of it

`FREEINK_FRONTLIGHT_LS` (`freeink-sdk/libs/hardware/FrontlightManager/src/FrontlightManager.cpp`)
runs the LEDC timer from RC_FAST (~17.5 MHz). RC_FAST keeps running in light sleep. The flag
also marks the channels `LEDC_SLEEP_MODE_KEEP_ALIVE` and keeps RC_FAST powered only while the
light is lit.

## Why it can't just be turned on

The X4 Pro profile drives the light at 25 kHz with 10-bit duty (`{8, 25000, 10, true, 9}` in
`BoardConfig.h`). LEDC needs `freq × 2^bits <= source clock`: 25 kHz × 1024 = 25.6 MHz, more
than RC_FAST gives. `ledc_timer_config()` fails, and the SDK leaves the light unconfigured.
With only the flag set, the light would be dead.

## Plan

1. Fork `Free-Ink/freeink-sdk` to `bee-san/freeink-sdk` and point the `freeink-sdk` submodule
   at it. Better still, offer the patch upstream so the fork can be dropped later.
2. In the `FREEINK_FRONTLIGHT_LS` `attachChannel()`, when the requested frequency does not fit
   RC_FAST at the requested resolution, lower the frequency to the highest that fits (about
   16 kHz at 10 bits) instead of failing. Keep 10 bits, so the dimmest night level is not
   coarser.
3. Add `-DFREEINK_FRONTLIGHT_LS=1` to the `x4pro` envs in `platformio.ini`.
4. In `enterDeepSleep()`, call the SDK's `FrontlightManager::park()` (through a new
   `HalFrontlight` method) before `startDeepSleep()`. With `KEEP_ALIVE` the pad's sleep
   isolation is off, and the SDK added `park()` for X4 Pro deep-sleep leakage on GPIO8/9.
   `begin()` already releases the pad hold on wake.
5. Remove the `Frontlight.isOn()` check from `canIdleLightSleep()`.

## Risks to check on the device

- Whine: the boost driver may be audible at 16 kHz. Listen in a quiet room at low and high
  brightness. If it whines, keep 25 kHz and use 9 bits instead (512 steps). The lowest level
  then becomes about twice as bright.
- Flicker or a brightness step on each sleep and wake.
- The light really is off in deep sleep (no faint glow), and it comes back after waking.
- Upstream merges: every SDK bump means rebasing the patch onto the fork.
