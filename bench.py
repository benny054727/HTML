"""Speed test for the face swap models on this phone. Only measures - changes nothing.

Run (stop the bot first so it doesn't compete for the CPU):
    ./stop.sh
    python bench.py
    ./run.sh --background

(Output is in English because Termux shows Hebrew reversed.)
"""

import gc
import glob
import os
import time
from pathlib import Path

import numpy as np
import onnxruntime as ort

M = Path(__file__).parent / "models"
REPS = 3


def cpu_info() -> tuple[int, int]:
    """(all cores, fast cores) - phones mix fast and slow cores."""
    cores = os.cpu_count() or 1
    freqs = []
    for f in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/cpuinfo_max_freq"):
        try:
            freqs.append(int(open(f).read()))
        except (OSError, ValueError):
            pass
    if not freqs:
        return cores, cores
    fast = sum(1 for f in freqs if f > (min(freqs) + max(freqs)) / 2) or cores
    return cores, fast


def session(name: str, threads: int):
    o = ort.SessionOptions()
    o.log_severity_level = 3
    o.intra_op_num_threads = threads  # 0 = onnxruntime decides (current bot behaviour)
    return ort.InferenceSession(str(M / name), o, providers=["CPUExecutionProvider"])


def timed(fn) -> float:
    fn()  # warm-up
    t = time.perf_counter()
    for _ in range(REPS):
        fn()
    return (time.perf_counter() - t) / REPS


def measure(threads: int) -> dict:
    rnd = np.random.default_rng(0)
    det = session("det_10g.onnx", threads)
    ins = session("inswapper_128.onnx", threads)
    hyp = session("hyperswap_1a_256.onnx", threads)
    emb = rnd.standard_normal((1, 512)).astype(np.float32)
    emb /= np.linalg.norm(emb)
    r = {
        "detect": timed(lambda: det.run(None, {det.get_inputs()[0].name: rnd.random((1, 3, 640, 640), np.float32)})),
        "inswapper": timed(lambda: ins.run(None, {"target": rnd.random((1, 3, 128, 128), np.float32), "source": emb})),
        "hyperswap": timed(lambda: hyp.run(None, {"target": rnd.random((1, 3, 256, 256), np.float32), "source": emb})),
    }
    del det, ins, hyp
    gc.collect()
    return r


def main() -> None:
    missing = [n for n in ("det_10g.onnx", "inswapper_128.onnx", "hyperswap_1a_256.onnx") if not (M / n).is_file()]
    if missing:
        raise SystemExit(f"Missing models: {', '.join(missing)}. Run: python download_models.py")

    cores, fast = cpu_info()
    print(f"CPU: {cores} cores ({fast} fast). onnxruntime {ort.__version__}")
    print("Measuring... (about 2-3 minutes, keep the screen on)\n")
    options = sorted({0, 1, 2, fast, cores} - {1} | ({1} if cores <= 2 else set()))

    rows = []
    for t in options:
        r = measure(t)
        full = r["detect"] + r["inswapper"] + r["hyperswap"]
        rows.append((t, r, full))
        label = "auto" if t == 0 else f"{t} threads"
        print(f"  {label:>10}: detect {r['detect']:.2f}s  inswapper {r['inswapper']:.2f}s  "
              f"hyperswap {r['hyperswap']:.2f}s  = {full:.2f}s per frame")

    best_t, best, best_full = min(rows, key=lambda x: x[2])
    auto_full = next(f for t, _, f in rows if t == 0)
    no_glasses = best["detect"] + best["hyperswap"]
    over = 1.15  # paste / colour / ffmpeg overhead
    print("\nRESULT")
    print(f"  best: {'auto' if best_t == 0 else str(best_t) + ' threads'} "
          f"({(1 - best_full / auto_full) * 100:.0f}% faster than now)")
    print("  Estimated time for a 15s video:")
    for name, per_frame, fps in [("now (15 fps, all stages)", auto_full, 15),
                                 ("best threads, 15 fps", best_full, 15),
                                 ("best threads, 10 fps", best_full, 10),
                                 ("best threads, 10 fps, no glasses stage", no_glasses, 10)]:
        print(f"    {name:<42} ~{per_frame * over * 15 * fps / 60:.0f} min")
    print("\nSend a screenshot of this screen to Claude.")


if __name__ == "__main__":
    main()
