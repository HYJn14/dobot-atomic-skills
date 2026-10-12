"""Record an explicit onsite reset; never moves robots or clears an owner lock."""

import argparse
import json
from pathlib import Path

from ..device_lease import DeviceLease
from ..models import load_scene_config
from .contracts import Catalog
from .store import Store


def reset_scene(directory, scene, mode="live", lease_path=None):
    # Both locks must be free: no gateway process and no legacy/quarantined owner.
    store = Store(directory)
    try:
        store.bind_mode(mode)
        with DeviceLease(lease_path, purpose="onsite-reset") as lease:
            store.reset_scene(Catalog(scene, mode).revision)
            if mode == "live":
                staging = lease.scene_stamp.with_suffix(".tmp")
                staging.write_text(json.dumps(store.scene_identity()), encoding="utf-8")
                staging.replace(lease.scene_stamp)
            if mode == "live" and lease.scene_marker.exists():
                lease.scene_marker.unlink()
    finally:
        store.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Record onsite reset of both homes, open grippers and A/B initial locations")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--scene", type=Path, default=Path(__file__).resolve().parents[1] / "scene_config.json")
    parser.add_argument("--lease-path", type=Path)
    parser.add_argument("--mode", choices=("mock", "live"), default="live")
    parser.add_argument("--confirm", choices=("RESET_VERIFIED",), required=True)
    args = parser.parse_args(argv)
    reset_scene(args.data_dir, load_scene_config(args.scene), args.mode, args.lease_path)
    print("Onsite reset recorded; execution and idempotency history retained. No hardware commands sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
