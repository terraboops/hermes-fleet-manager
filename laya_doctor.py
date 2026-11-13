"""`laya doctor`: what hardware was found, which backend was chosen, and why.

    laya.py doctor              pick a backend exactly as the gate would (loads the real model)
    laya.py doctor --dry-run    force the stub backend; no model is loaded
    laya.py doctor --json       the same report as JSON

Exit 0 when a backend or model-less mode was determined, 1 when the check itself broke.
"""
import argparse
import json
import sys

import laya_backend_manager as lbm


def _hardware_lines(hw):
    if hw is None:
        return ["  (hardware detection failed)"]
    cpu = hw.cpu_model or hw.machine or "unknown cpu"
    ram = f"{hw.ram_bytes / 2**30:.0f} GiB" if hw.ram_bytes else "unknown RAM"
    lines = [f"  {'Apple Silicon' if hw.apple_silicon else 'not Apple Silicon'}: {cpu}, "
             f"{hw.cpu_cores or '?'} cores, {ram} ({hw.system or '?'}/{hw.machine or '?'})"]
    for label, probe in (("nvidia", hw.nvidia), ("rocm", hw.rocm), ("vulkan", hw.vulkan)):
        lines.append(f"  {label}: " + ("available" + (f" ({probe.gpu_name})" if probe.gpu_name else "")
                                       if probe.available else f"absent ({probe.reason or 'not found'})"))
    return lines


def report(dry_run=False):
    """(hardware, status, source_label) for this machine."""
    try:
        from laya_hardware import detect_hardware
        hw = detect_hardware()
    except Exception:
        hw = None
    if dry_run:
        mgr = lbm.BackendManager(hw=hw, env={lbm.ENV_VAR: "stub"}, config_value=None)
    else:
        mgr = lbm.BackendManager(hw=hw)
    status = mgr.start()
    source = "--dry-run" if dry_run else status.override_source
    return hw, status, source


def render(hw, status, source, dry_run):
    out = ["hardware:"] + _hardware_lines(hw)
    out.append("")
    if status.model_less:
        out.append("backend: none (model-less mode: lanes 1 and 2 run by rule, lane 3 shows the event)")
    else:
        out.append(f"backend: {status.backend}   (loaded in {status.load_ms} ms)"
                   + ("   [dry-run: no real model loaded]" if dry_run else ""))
    out.append("")
    out.append("why:")
    out.append(f"  override: {status.override} (source: {source})")
    if status.override_error:
        out.append(f"  override error: {status.override_error}")
    if status.hardware_error:
        out.append(f"  hardware error: {status.hardware_error}")
    for e in status.fallback_chain:
        out.append(f"  rung {e['rung']}: {e['status']}" + (f" - {e['reason']}" if e["reason"] else ""))
    if not status.fallback_chain:
        out.append("  (no rungs attempted)")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="laya doctor", description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="force the stub backend; load no model")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    args = ap.parse_args(argv)
    try:
        hw, status, source = report(args.dry_run)
    except Exception as e:
        print(f"laya doctor failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({"hardware": hw.to_dict() if hw else None, "status": status.to_dict(),
                          "override_source": source, "dry_run": args.dry_run}, indent=2))
    else:
        print(render(hw, status, source, args.dry_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
