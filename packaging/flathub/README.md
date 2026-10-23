# Flathub submission deferred

TuneConsole does not maintain a Flathub application submission. The obsolete manifest and
duplicated dependency snapshot have been removed. The maintained Linux distribution is the
signed, self-hosted Flatpak repository described in [Packaging](../README.md#linux--flatpak).

The repository maintainer owns that release path. Reopening Flathub distribution requires an
assigned maintainer and generation of the intended release's version, immutable source revision,
wheel checksum, locked dependencies, and AppStream release metadata as one validated input set.
Do not describe a submission as ready until it builds offline on the supported runtime and
architectures and its immutable release references have been checked.
