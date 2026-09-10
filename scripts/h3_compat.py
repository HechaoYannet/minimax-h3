"""h3_compat.py -- environment compatibility shims for the MiniMax-H3 stack.

Must be imported BEFORE torchaudio / transformers / diffsynth.

STATUS ON THIS BOX: NOT NEEDED.  torchaudio 2.11.0+cu132 is installed, so
"import torchaudio" succeeds on its own and apply() reports "already
importable" without touching anything.  The shim is kept as a fallback for a
fresh environment that ends up with the PyPI cu130 build.

The real fix (what this box now uses)
-------------------------------------
torchaudio IS published for cu132 -- but on the **test** channel, not the
stable one:

    pip3 install torch torchvision torchaudio \
        --index-url https://download.pytorch.org/whl/test/cu132

The stable https://download.pytorch.org/whl/cu132 index carries torch and
torchvision only; adding torchaudio there fails outright with
"Could not find a version that satisfies the requirement torchaudio".
Getting torchaudio from the *default* PyPI index instead installs the cu130
build, which is what previously broke "import torchaudio":

    RuntimeError: Detected that PyTorch and TorchAudio were compiled with
    different CUDA versions. PyTorch has CUDA version 13.2 whereas TorchAudio
    has CUDA version 13.0.

That is fatal here because transformers imports torchaudio inside
audio_utils.py, diffsynth.utils.data.audio imports it at module level, and the
H3 pipeline imports that.  Nothing in the pipeline can even be constructed.

Caveat worth remembering: 2.11.0+cu132 is the cu130 build **repackaged**.  Its
three .so files are md5-identical to the cu130 wheel and it still reports
torch.ops._torchaudio.cuda_version() == 13000; the wheel just ships

    pass  # CUDA version mismatch check disabled by repackage_torchaudio_cu130_to_cu132.py

in _extension/utils.py in place of the check.  So the ABI is unchanged -- what
you gain is the gate removed for you, not a real CUDA 13.2 compilation.
(pytorch/pytorch#183336: the release Dockerfile skips torchaudio when
CUDA_PATH=cu132.)

Note on the gate itself: it compares

    ta_version = f"{version_str[:-3]}.{version_str[-2]}"      # 13020 -> "13.2"

against torch.version.cuda -- a SINGLE-character slice, so a genuinely matched
build does pass.  An earlier version of this docstring wrote [-2:] ("13.20")
and concluded the check could never pass; that was wrong.

What this module does (fallback path)
-------------------------------------
It pre-seeds sys.modules['torchaudio._extension.utils'] with the real module
loaded from disk, but with _check_cuda_version replaced by a corrected version,
so that torchaudio/__init__.py executes normally and binds the patched
function.  Nothing on disk is modified.  The real
torch.ops._torchaudio.cuda_version() value is still read and reported; a
genuine major-version mismatch is raised as a warning rather than ignored.

After importing, verify() exercises the compiled extension so a real ABI
problem fails loudly here rather than deep inside a generation run.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import warnings

_PATCHED = False


def _find_torchaudio_utils():
    import site
    roots = []
    try:
        roots += list(site.getsitepackages())
    except Exception:
        pass
    try:
        roots.append(site.getusersitepackages())
    except Exception:
        pass
    for root in roots:
        cand = os.path.join(root, "torchaudio", "_extension", "utils.py")
        if os.path.exists(cand):
            return cand
    return None


def apply():
    """Make 'import torchaudio' work.  Returns a short status string."""
    global _PATCHED
    try:
        import torchaudio  # noqa: F401
        return "torchaudio already importable (no shim needed)"
    except ModuleNotFoundError:
        return "torchaudio is not installed"
    except RuntimeError as exc:
        if "compiled with different CUDA versions" not in str(exc):
            raise
        reason = str(exc).strip().splitlines()[-1]
    except Exception:
        raise

    if _PATCHED:
        return "already patched"

    path = _find_torchaudio_utils()
    if path is None:
        raise ImportError("could not locate torchaudio/_extension/utils.py to patch")

    # Load the real utils.py standalone: it imports only stdlib + torch at module
    # level, so this is safe and does not trigger torchaudio/__init__.py.
    spec = importlib.util.spec_from_file_location("torchaudio._extension.utils", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def _check_cuda_version_fixed():
        """Corrected torchaudio check: compare parsed numeric versions."""
        version = module.torch.ops._torchaudio.cuda_version()
        if version is None or module.torch.version.cuda is None:
            return version
        s = str(version)
        ta = (int(s[:-3]), int(s[-2:])) if len(s) > 3 else (int(s), 0)
        parts = module.torch.version.cuda.split(".")
        tv = (int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
        if ta[0] != tv[0]:
            warnings.warn(
                "torchaudio was built for CUDA %d.%d but torch uses CUDA %d.%d: "
                "major-version mismatch, the compiled extension may be "
                "ABI-incompatible." % (ta[0], ta[1], tv[0], tv[1]),
                RuntimeWarning, stacklevel=2)
        # Same major version, only the minor differs: the stock check rejects this
        # unconditionally (see module docstring) but the extension loads and runs.
        return version

    module._check_cuda_version = _check_cuda_version_fixed
    sys.modules["torchaudio._extension.utils"] = module
    _PATCHED = True

    import torchaudio  # noqa: F401  -- now executes __init__ normally

    ext = sys.modules.get("torchaudio._extension")
    if ext is not None and getattr(ext, "_check_cuda_version", None) is not _check_cuda_version_fixed:
        try:
            ext._check_cuda_version = _check_cuda_version_fixed
        except Exception:
            pass
    return "patched torchaudio CUDA check (%s)" % reason.split("Please")[0].strip()


def verify(verbose=True):
    """Prove the compiled torchaudio extension actually works."""
    import torch
    import torchaudio
    import torchaudio.functional as F

    # torchaudio.__version__ is the *compiled* tag the loaded extension reports
    # (so the +cu132 wheel still says +cu130 -- it ships the cu130 .so).  The
    # installed distribution name is a different thing; report both so the
    # difference is never mistaken for a broken install.
    try:
        from importlib.metadata import version as _dist_version
        dist = _dist_version("torchaudio")
    except Exception:
        dist = "unknown"
    built = torch.ops._torchaudio.cuda_version()
    info = {
        "torch": torch.__version__,
        "torch_cuda": torch.version.cuda,
        "torchaudio_dist": dist,
        "torchaudio_runtime_tag": torchaudio.__version__,
        "torchaudio_built_cuda": built,
        "align_available": bool(torch.ops._torchaudio.is_align_available()),
    }
    if dist != torchaudio.__version__:
        info["note"] = ("distribution is %s but the loaded extension reports %s; "
                        "expected for the repackaged cu132 wheel (see module "
                        "docstring)" % (dist, torchaudio.__version__))
    x = torch.randn(2, 48000)
    info["resample_48k_to_32k"] = tuple(F.resample(x, 48000, 32000).shape)
    info["resample_same_rate_identity"] = bool(torch.equal(x, F.resample(x, 48000, 48000)))
    info["resample_16k_to_32k"] = tuple(F.resample(torch.randn(2, 16000), 16000, 32000).shape)
    info["finite"] = bool(torch.isfinite(F.resample(x, 48000, 32000)).all())
    if verbose:
        print("[h3_compat] torchaudio extension verified:")
        for k, v in info.items():
            print("    %-28s %s" % (k, v))
    return info


def apply_and_verify(verbose=True):
    status = apply()
    if verbose:
        print("[h3_compat] " + status)
    info = verify(verbose=verbose)
    info["status"] = status
    return info


if __name__ == "__main__":
    apply_and_verify()
