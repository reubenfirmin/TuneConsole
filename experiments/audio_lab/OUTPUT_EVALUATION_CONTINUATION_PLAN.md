# Declared continuation after the #587 publication timeout

The metadata arm of round 2 hit the original 180-second publication budget
while staging typed candidate pools. Its failed report and private database
remain intact. No complete consumer output or quality score had been observed.

Run a separate diagnostic round against exactly the same frozen SQL, referenced
files, samples, six specialists, shared intensity artifact, clock and seed. Use
600 seconds per publication, 30 seconds per consumer operation and 1,800 seconds
per arm, single-threaded and one arm at a time. These match the earlier #587
library-replay approach documented in `EXTERNAL_EXPERIMENTS.md`. Record every
actual duration against both the original and extended limits. Completing this
diagnostic cannot pass or erase the original 180-second failure.

All evidence ablations, reference semantics, restrictions, repetitions and
unknown-label treatment remain as declared in `OUTPUT_EVALUATION_PLAN.md`.
The purpose is to obtain the paired complete outputs without mistaking a
publication timeout for a model-quality result. No model fitting, target-based
selection, live-account write, runtime change or new listening gate is added.
