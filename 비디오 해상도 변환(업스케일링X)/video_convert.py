#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from queue import Queue
from typing import Optional, Tuple

from tqdm import tqdm

# =========================
# 사용자 설정
# =========================
INPUT_DIR = r".\MyVideos"          # 원본 동영상 폴더
OUTPUT_DIR = r".\ConvertedVideo"    # 결과 저장 폴더
WORKERS = 3                         # 최대 2 권장
FORCE_CPU = False                   # True면 GPU 사용 안 함
OVERWRITE = True                   # True면 기존 파일 덮어쓰기

# 오디오 설정
AUDIO_BITRATE_K = 192               # AAC 오디오 비트레이트(kbps)

SUPPORTED_EXTENSIONS = {
    ".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv",
    ".m4v", ".ts", ".mts", ".m2ts", ".webm"
}

TARGET_LONG_SIDE = 1920
MAX_WORKERS = 2

# 비트레이트 기준
# 1920x1080 -> 7500k
# 1920x1920 -> 9000k
MIN_PIXELS = 1920 * 1080
MAX_PIXELS = 1920 * 1920
MIN_BITRATE_K = 7500
MAX_BITRATE_K = 9000

# Ctrl+C 종료 제어
SHUTDOWN_EVENT = threading.Event()
ACTIVE_PROCESSES: set[subprocess.Popen] = set()
ACTIVE_PROCESSES_LOCK = threading.Lock()


def register_process(process: subprocess.Popen) -> None:
    with ACTIVE_PROCESSES_LOCK:
        ACTIVE_PROCESSES.add(process)


def unregister_process(process: subprocess.Popen) -> None:
    with ACTIVE_PROCESSES_LOCK:
        ACTIVE_PROCESSES.discard(process)


def kill_all_active_processes() -> None:
    with ACTIVE_PROCESSES_LOCK:
        processes = list(ACTIVE_PROCESSES)

    for proc in processes:
        try:
            if proc.poll() is None:
                proc.kill()
        except Exception:
            pass


def run_command(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace"
    )


def check_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        print("[오류] ffmpeg를 찾을 수 없습니다. PATH에 등록해주세요.", file=sys.stderr)
        sys.exit(1)

    if shutil.which("ffprobe") is None:
        print("[오류] ffprobe를 찾을 수 없습니다. PATH에 등록해주세요.", file=sys.stderr)
        sys.exit(1)


def has_hevc_nvenc() -> bool:
    result = run_command(["ffmpeg", "-hide_banner", "-encoders"])
    text = (result.stdout or "") + "\n" + (result.stderr or "")
    return "hevc_nvenc" in text


def probe_video_info(file_path: Path) -> Tuple[int, int]:
    cmd = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "json",
        str(file_path)
    ]
    result = run_command(cmd)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe 실패: {result.stderr.strip()}")

    try:
        data = json.loads(result.stdout)
        stream = data["streams"][0]
        width = int(stream["width"])
        height = int(stream["height"])
        if width <= 0 or height <= 0:
            raise ValueError("유효하지 않은 해상도")
        return width, height
    except Exception as e:
        raise RuntimeError(f"비디오 정보 파싱 실패: {e}")


def probe_video_duration(file_path: Path) -> Optional[float]:
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(file_path)
    ]
    result = run_command(cmd)
    if result.returncode != 0:
        return None

    try:
        duration = float(result.stdout.strip())
        return duration if duration > 0 else None
    except Exception:
        return None


def parse_ffmpeg_time_to_seconds(value: str) -> float:
    try:
        hh, mm, ss = value.strip().split(":")
        return int(hh) * 3600 + int(mm) * 60 + float(ss)
    except Exception:
        return 0.0


def make_even(value: int) -> int:
    if value < 2:
        value = 2
    return value if value % 2 == 0 else value - 1


def calculate_scaled_resolution(
    width: int,
    height: int,
    target_long_side: int = TARGET_LONG_SIDE
) -> Tuple[int, int]:
    """
    긴 변이 1920보다 큰 경우에만 축소.
    긴 변이 1920 이하이면 원본 해상도 유지.
    """
    long_side = max(width, height)

    if long_side <= target_long_side:
        return make_even(width), make_even(height)

    scale_ratio = target_long_side / long_side
    new_width = make_even(int(round(width * scale_ratio)))
    new_height = make_even(int(round(height * scale_ratio)))

    return new_width, new_height


def calculate_bitrate_k(width: int, height: int) -> int:
    """
    출력 해상도 기준 선형 비트레이트 계산

    - 1920x1080 이하: 7500k
    - 1920x1920 이상: 9000k
    - 그 사이는 선형 보간
    """
    pixels = width * height

    if pixels <= MIN_PIXELS:
        return MIN_BITRATE_K

    if pixels >= MAX_PIXELS:
        return MAX_BITRATE_K

    ratio = (pixels - MIN_PIXELS) / (MAX_PIXELS - MIN_PIXELS)
    bitrate = MIN_BITRATE_K + ratio * (MAX_BITRATE_K - MIN_BITRATE_K)
    return int(round(bitrate))

def get_video_bitrate(source_file: Path) -> int:

    result = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=bit_rate",
            "-of", "default=noprint_wrappers=1:nokey=1",
            source_file
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    value = result.stdout.strip()


    if value and value != "N/A":
        return int(round(float(value)/ 1000))

    return MAX_BITRATE_K


def build_output_path(input_root: Path, output_root: Path, source_file: Path) -> Path:
    relative = source_file.relative_to(input_root)
    return output_root / relative.with_suffix(".mp4")


def collect_video_files(input_root: Path) -> list[Path]:
    files = []
    for p in input_root.rglob("*"):
        if SHUTDOWN_EVENT.is_set():
            break
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS:
            files.append(p)
    return files


def shorten_text(text: str, max_len: int = 50) -> str:
    if len(text) <= max_len:
        return text
    return "..." + text[-(max_len - 3):]


def build_video_codec_args(use_nvenc: bool, bitrate_k: int) -> Tuple[list[str], str]:
    """
    HEVC CBR 인코더 옵션 생성
    """
    if use_nvenc:
        args = [
            "-c:v", "hevc_nvenc",
            "-preset", "p5",
            "-rc", "cbr",
            "-b:v", f"{bitrate_k}k",
            "-minrate", f"{bitrate_k}k",
            "-maxrate", f"{bitrate_k}k",
            "-bufsize", f"{bitrate_k * 2}k",
            "-profile:v", "main",
            "-pix_fmt", "yuv420p"
        ]
        name = f"hevc_nvenc CBR({bitrate_k}k)"
    else:
        args = [
            "-c:v", "libx265",
            "-preset", "medium",
            "-b:v", f"{bitrate_k}k",
            "-profile:v", "main",
            "-pix_fmt", "yuv420p",
            "-x265-params",
            f"vbv-maxrate={bitrate_k}:vbv-bufsize={bitrate_k * 2}:strict-cbr=1:nal-hrd=cbr:log-level=error"
        ]
        name = f"libx265 CBR({bitrate_k}k)"
    return args, name


def encode_with_progress(
    source_file: Path,
    output_file: Path,
    out_w: int,
    out_h: int,
    duration: Optional[float],
    use_nvenc: bool,
    overwrite: bool,
    progress_bar: tqdm,
    phase_desc: str,
    bitrate_k: int
) -> Tuple[int, str, str]:
    """
    return: (return_code, stderr_text, encoder_name)
    """
    if SHUTDOWN_EVENT.is_set():
        return 1, "사용자에 의해 취소됨", "cancelled"

    progress_bar.reset(total=100)
    progress_bar.n = 0
    progress_bar.set_description_str(phase_desc)
    progress_bar.refresh()

    video_codec_args, encoder_name = build_video_codec_args(use_nvenc, bitrate_k)
    vf = f"scale={out_w}:{out_h}"

    cmd = [
        "ffmpeg",
        "-y" if overwrite else "-n",
        "-hide_banner",
        "-loglevel", "error",
        "-nostats",
        "-progress", "pipe:1",
        "-i", str(source_file),
        "-map", "0:v:0",
        "-map", "0:a?",
        "-vf", vf,
        *video_codec_args,
        "-c:a", "aac",
        "-b:a", f"{AUDIO_BITRATE_K}k",
        "-movflags", "+faststart",
        str(output_file)
    ]

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1
    )
    register_process(process)

    last_percent = 0.0
    pulse = 0.0

    try:
        if process.stdout is not None:
            for raw_line in process.stdout:
                if SHUTDOWN_EVENT.is_set():
                    if process.poll() is None:
                        process.kill()
                    break

                line = raw_line.strip()
                if not line:
                    continue

                if line.startswith("out_time=") and duration:
                    current_time_str = line.split("=", 1)[1]
                    current_sec = parse_ffmpeg_time_to_seconds(current_time_str)
                    percent = min(100.0, max(0.0, (current_sec / duration) * 100.0))

                    if percent > last_percent:
                        progress_bar.update(percent - last_percent)
                        last_percent = percent

                elif line.startswith("progress=") and duration is None:
                    pulse = (pulse + 5.0) % 100.0
                    progress_bar.n = pulse
                    progress_bar.refresh()

                elif line == "progress=end":
                    break

        stderr_text = ""
        if process.stderr is not None:
            stderr_text = process.stderr.read()

        return_code = process.wait()

        if SHUTDOWN_EVENT.is_set():
            return 1, "사용자에 의해 취소됨", encoder_name

        if return_code == 0 and progress_bar.n < 100:
            progress_bar.update(100 - progress_bar.n)

        return return_code, stderr_text.strip(), encoder_name

    except Exception:
        if process.poll() is None:
            process.kill()
        raise
    finally:
        unregister_process(process)




def convert_video(
    source_file: Path,
    input_root: Path,
    output_root: Path,
    use_nvenc: bool,
    overwrite: bool,
    progress_slots: Queue
) -> Tuple[Path, bool, str]:
    progress_bar = None
    slot = None

    try:
        if SHUTDOWN_EVENT.is_set():
            return source_file, False, 0, 0, "사용자에 의해 취소됨"

        width, height = probe_video_info(source_file)
        duration = probe_video_duration(source_file)
        out_w, out_h = calculate_scaled_resolution(width, height)
        bitrate_calc = calculate_bitrate_k(out_w, out_h)
        bitrate_origin = get_video_bitrate(source_file)
        bitrate_k = min(bitrate_origin, bitrate_calc)

        output_file = build_output_path(input_root, output_root, source_file)
        output_file.parent.mkdir(parents=True, exist_ok=True)

        if output_file.exists():
            return source_file, True, bitrate_origin, bitrate_k, f"건너뜀(이미 존재): {output_file}"
        
        if bitrate_origin <= bitrate_k:
            shutil.copy2(source_file, output_file)
            return source_file, True, bitrate_origin, bitrate_k, f"건너뜀(비트레이트 낮음): {output_file}"

        relative_name = str(source_file.relative_to(input_root))
        display_name = shorten_text(relative_name, 44)

        slot = progress_slots.get()
        progress_bar = tqdm(
            total=100,
            desc=display_name,
            position=slot,
            leave=False,
            dynamic_ncols=True,
            mininterval=0.1,
            bar_format="{desc:<52} |{bar}| {percentage:6.2f}%"
        )

        rc, err, encoder_name = encode_with_progress(
            source_file=source_file,
            output_file=output_file,
            out_w=out_w,
            out_h=out_h,
            duration=duration,
            use_nvenc=use_nvenc,
            overwrite=overwrite,
            progress_bar=progress_bar,
            phase_desc=f"{display_name} [{bitrate_k}k]",
            bitrate_k=bitrate_k
        )

        if SHUTDOWN_EVENT.is_set():
            try:
                if output_file.exists():
                    output_file.unlink()
            except Exception:
                pass
            return source_file, False, bitrate_origin, bitrate_k, "사용자에 의해 취소됨"

        if rc != 0:
            try:
                if output_file.exists():
                    output_file.unlink()
            except Exception:
                pass
            return source_file, False, bitrate_origin, bitrate_k, f"변환 실패: {err}"

        return (
            source_file,
            True,
            bitrate_origin,
            bitrate_k,
            f"성공 -> {output_file} | {width}x{height} -> {out_w}x{out_h} | {encoder_name}"
            
        )

    except Exception as e:
        return source_file, False, bitrate_origin, bitrate_k, f"예외 발생: {e}"

    finally:
        if progress_bar is not None:
            progress_bar.close()
        if slot is not None:
            progress_slots.put(slot)


def main() -> None:
    check_ffmpeg()

    input_root = Path(INPUT_DIR).resolve()
    output_root = Path(OUTPUT_DIR).resolve()

    if not input_root.exists() or not input_root.is_dir():
        print(f"[오류] 입력 폴더를 찾을 수 없습니다: {input_root}", file=sys.stderr)
        sys.exit(1)

    workers = min(max(1, WORKERS), MAX_WORKERS)

    nvenc_available = has_hevc_nvenc()
    use_nvenc = nvenc_available and not FORCE_CPU

    video_files = collect_video_files(input_root)
    if not video_files:
        print("처리할 동영상 파일이 없습니다.")
        return

    tqdm.set_lock(threading.RLock())

    progress_slots = Queue()
    for pos in range(1, workers + 1):
        progress_slots.put(pos)

    tqdm.write("=" * 80)
    tqdm.write(f"입력 폴더 : {input_root}")
    tqdm.write(f"출력 폴더 : {output_root}")
    tqdm.write(f"병렬 처리 : {workers}개 스레드")
    tqdm.write(f"인코더    : {'hevc_nvenc CBR' if use_nvenc else 'libx265 CBR'}")
    tqdm.write(
        f"비트레이트 규칙: "
        f"{MIN_PIXELS} px 이하 -> {MIN_BITRATE_K}k, "
        f"{MAX_PIXELS} px 이상 -> {MAX_BITRATE_K}k, "
        f"그 사이는 선형 계산"
    )
    tqdm.write(f"총 파일 수: {len(video_files)}")
    tqdm.write("=" * 80)

    success_count = 0
    fail_count = 0
    executor: Optional[ThreadPoolExecutor] = None
    futures = []

    try:
        with tqdm(
            total=len(video_files),
            desc="전체 진행",
            position=0,
            leave=True,
            dynamic_ncols=True,
            unit="file"
        ) as overall_bar:
            executor = ThreadPoolExecutor(max_workers=workers)

            futures = [
                executor.submit(
                    convert_video,
                    source_file,
                    input_root,
                    output_root,
                    use_nvenc,
                    OVERWRITE,
                    progress_slots
                )
                for source_file in video_files
            ]

            for future in as_completed(futures):
                if SHUTDOWN_EVENT.is_set():
                    break

                source_file, success, bitrate_origin, bitrate_result, message = future.result()

                if message == "사용자에 의해 취소됨":
                    break

                overall_bar.update(1)

                if success:
                    success_count += 1
                    tqdm.write(f"[OK] {source_file}")
                    tqdm.write(f"     {message}")
                    tqdm.write(f"     비트 변환 : {bitrate_origin} -> {bitrate_result}")
                else:
                    fail_count += 1
                    tqdm.write(f"[FAIL] {source_file}")
                    tqdm.write(f"       {message}")

    except KeyboardInterrupt:
        SHUTDOWN_EVENT.set()
        tqdm.write("")
        tqdm.write("[중단] Ctrl+C 감지 - 실행 중인 모든 작업을 종료합니다.")
        kill_all_active_processes()

        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

        for future in futures:
            future.cancel()

        tqdm.write("[중단] 프로그램을 종료합니다.")
        sys.exit(130)

    finally:
        if SHUTDOWN_EVENT.is_set():
            kill_all_active_processes()
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)

    tqdm.write("=" * 80)
    tqdm.write(f"완료 - 성공: {success_count}, 실패: {fail_count}")
    tqdm.write(f"결과 폴더: {output_root}")
    tqdm.write("=" * 80)


if __name__ == "__main__":
    main()
