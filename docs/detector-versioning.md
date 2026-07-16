# Motion detector versioning

Motion sidecars are experimental data products, so each one must say exactly
what produced it. Behaviour is **code × knobs**:

- **code** — the detector implementation, named by a version id such as
  `blob-track-v1`, recorded in the sidecar as `analysis`.
- **knobs** — that detector's tuning parameters, recorded in the sidecar as
  `params`, always fully resolved (defaults included, not just overrides).

That pair identifies a run. Neither alone does: two sidecars can both say
`blob-track-v1` and mean different things if their `bg_alpha` differs.

## Which axis do I change?

**Retuning a knob is not a new version.** Sweep knobs freely — that is the
routine work, and every result stays self-describing because `params` records
what was used. Add a new detector version only for a genuine algorithm change:
different maths, different output shape, a new knob that older sidecars have no
value for.

The knobs of a detector are simply its defaulted parameters, so the function
signature is the single source of truth for their names, types and defaults.
Nothing has to be kept in sync by hand.

```
python -m camrig.motion --help     # every detector's knobs and defaults
```

## Sweeping knobs offline

The stable entry point resolves and records knobs for you:

```python
analyse(stream, width, height, detector="blob-track-v1", bg_alpha=0.02)
```

From the shell, over a retained MKV, writing each run to its own filename so
results do not overwrite one another:

```bash
for a in 0.01 0.05 0.2; do
    ffmpeg -i clip.mkv -vf scale=728:544,format=gray -f rawvideo - |
        python3 -m camrig.motion --width 728 --height 544 \
            --param bg_alpha=$a -o clip.motion.bg$a.json
done
```

Unknown knob names and uncoercible values are rejected before any frame is
read, so a typo costs nothing.

## Deploying

```toml
[postprocess]
motion_detector = "blob-track-v1"

[postprocess.motion_params."blob-track-v1"]
bg_alpha = 0.05
```

Every knob may be omitted; anything absent uses the detector's own default, so
deleting the table means "stock". Tables for detectors that are not selected are
ignored, so tuning for several detectors can sit in the file side by side. Both
the detector id and the knobs are validated at config load — a typo fails
immediately rather than after ffmpeg has decoded a clip.

The operational sidecar filename stays `<clip>.motion.json`. Rebuild it after a
change with `camrig postprocess --force`.

## Adding a version

1. Copy the current implementation to a new function, e.g.
   `analyse_blob_track_v2`.
2. Make the algorithm change only in the new function; give any new knob a
   default in its signature.
3. Register `"blob-track-v2"` in `DETECTORS`.
4. Add synthetic regression tests for the behaviour it changes.
5. Replay retained MKVs through both versions, to distinct output filenames.
6. Switch `postprocess.motion_detector` once it should become the on-device
   detector.

## When does a version become immutable?

**When the first sidecar you intend to keep has been written with it** — that is
what an `analysis` id has to keep meaning. Before any field data exists a version
id is just a name, and freezing it early only canonises known-bad behaviour into
a lineage you then carry forever. Until the rig is producing keepable clips, edit
the current detector in place.
