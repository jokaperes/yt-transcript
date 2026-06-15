import json
import textwrap
from pathlib import Path

from transcribe_youtube import (
    OUTPUT_FORMATS,
    _extract_wheel_dlls,
    download_cache_dir,
    ensure_cuda_runtime,
    normalize_runtime_for_platform,
    parse_urls,
    purge_download_cache,
    safe_cuda_compute_type,
    safe_name,
    timestamp,
    write_json,
    write_md,
    write_srt,
    write_txt,
    write_vtt,
    normalize_cookies_file,
)


def test_safe_name_basic() -> None:
    assert safe_name("Hello World") == "Hello World"
    assert safe_name("") == "youtube-video"
    assert safe_name("a" * 200) == "a" * 100


def test_safe_name_strips_special_chars() -> None:
    assert safe_name('Video: "Best" <Part> 1/2 | test?') == "Video Best Part 12 test"


def test_safe_name_strips_unicode_symbols() -> None:
    assert safe_name("Olá \u2605 mundo!") == "Olá mundo"


def test_safe_name_strips_control_chars() -> None:
    assert safe_name("hello\x00world") == "helloworld"


def test_safe_name_trims_dots_spaces() -> None:
    assert safe_name("  hello . world .  ") == "hello . world"


def test_timestamp_zero() -> None:
    assert timestamp(0.0) == "00:00:00,000"


def test_timestamp_srt_format() -> None:
    assert timestamp(3661.50, vtt=False) == "01:01:01,500"


def test_timestamp_vtt_format() -> None:
    assert timestamp(3661.50, vtt=True) == "01:01:01.500"


def test_timestamp_rounding() -> None:
    assert timestamp(0.9999) == "00:00:01,000"


def test_write_srt(tmp_path: Path) -> None:
    segments = [
        {"start": 1.0, "end": 3.5, "text": "Hello world"},
        {"start": 4.0, "end": 6.0, "text": "Second line"},
    ]
    out = tmp_path / "test.srt"
    write_srt(out, segments)
    content = out.read_text(encoding="utf-8")
    assert "1\n00:00:01,000 --> 00:00:03,500\nHello world" in content
    assert "2\n00:00:04,000 --> 00:00:06,000\nSecond line" in content


def test_write_md(tmp_path: Path) -> None:
    segments = [
        {"start": 0.0, "end": 2.0, "text": " Olá pessoal."},
        {"start": 2.1, "end": 4.0, "text": " Bem-vindos."},
        {"start": 10.0, "end": 12.0, "text": " Novo parágrafo."},
    ]
    info = {
        "title": "Meu Vídeo",
        "id": "abc123",
        "uploader": "Canal",
        "upload_date": "20260610",
        "duration": 754,
    }
    out = tmp_path / "video.pt.md"
    write_md(out, segments, info)
    content = out.read_text(encoding="utf-8")
    assert content.startswith("# Meu Vídeo")
    assert "- **Channel:** Canal" in content
    assert "- **URL:** https://www.youtube.com/watch?v=abc123" in content
    assert "- **Uploaded:** 2026-06-10" in content
    assert "- **Duration:** 00:12:34" in content
    assert "## Transcript" in content
    # Segments 1+2 merge into one paragraph; the >2s gap starts a new one.
    assert "**[00:00:00]** Olá pessoal. Bem-vindos." in content
    assert "**[00:00:10]** Novo parágrafo." in content


def test_write_md_no_info(tmp_path: Path) -> None:
    out = tmp_path / "video.pt.md"
    write_md(out, [{"start": 0.0, "end": 1.0, "text": "Oi"}], {})
    content = out.read_text(encoding="utf-8")
    assert content.startswith("# video.pt")
    assert "**[00:00:00]** Oi" in content


def test_write_vtt(tmp_path: Path) -> None:
    segments = [
        {"start": 0.0, "end": 2.0, "text": "First"},
        {"start": 2.5, "end": 5.0, "text": "Second"},
    ]
    out = tmp_path / "test.vtt"
    write_vtt(out, segments)
    content = out.read_text(encoding="utf-8")
    assert content.startswith("WEBVTT")
    assert "00:00:00.000 --> 00:00:02.000\nFirst" in content
    assert "00:00:02.500 --> 00:00:05.000\nSecond" in content


def test_write_txt_with_timestamps(tmp_path: Path) -> None:
    segments = [
        {"start": 1.0, "end": 3.0, "text": "Hello"},
        {"start": 3.5, "end": 5.0, "text": "world"},
    ]
    out = tmp_path / "test.txt"
    write_txt(out, segments)
    content = out.read_text(encoding="utf-8")
    assert "[00:00:01,000]" in content
    assert "Hello" in content
    assert "world" in content


def test_write_txt_paragraph_breaks(tmp_path: Path) -> None:
    segments = [
        {"start": 1.0, "end": 3.0, "text": "First chunk"},
        {"start": 3.5, "end": 5.0, "text": "continues"},
        {"start": 12.0, "end": 15.0, "text": "New paragraph"},
    ]
    out = tmp_path / "test.txt"
    write_txt(out, segments)
    content = out.read_text(encoding="utf-8")
    assert "\n\n" in content


def test_write_txt_empty_segments(tmp_path: Path) -> None:
    segments = [
        {"start": 1.0, "end": 2.0, "text": "  "},
        {"start": 2.5, "end": 4.0, "text": "Actual text"},
    ]
    out = tmp_path / "test.txt"
    write_txt(out, segments)
    content = out.read_text(encoding="utf-8")
    assert "Actual text" in content


def test_write_json(tmp_path: Path) -> None:
    segments = [
        {"id": 0, "start": 1.0, "end": 3.0, "text": "Hello"},
    ]
    info = {"title": "Test Video", "id": "abc123", "duration": 60.0}
    out = tmp_path / "test.json"
    write_json(out, segments, info)
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["segments"][0]["text"] == "Hello"
    assert data["info"]["title"] == "Test Video"
    assert data["info"]["id"] == "abc123"


def test_write_json_strips_unknown_info(tmp_path: Path) -> None:
    segments = [{"id": 0, "start": 0.0, "end": 1.0, "text": "X"}]
    info = {"title": "T", "extra_field": "ignored"}
    out = tmp_path / "test.json"
    write_json(out, segments, info)
    data = json.loads(out.read_text(encoding="utf-8"))
    assert "extra_field" not in data["info"]
    assert data["info"]["title"] == "T"


def test_normalize_cookies_no_change(tmp_path: Path) -> None:
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("youtube.com\tFALSE\t/\tFALSE\t0\tVISITOR_INFO1_LIVE\txyz\n", encoding="utf-8")
    result = normalize_cookies_file(str(cookies), tmp_path)
    assert result == str(cookies)


def test_normalize_cookies_fixes_dot_prefix(tmp_path: Path) -> None:
    cookies = tmp_path / "cookies.txt"
    cookies.write_text(
        ".youtube.com\tFALSE\t/\tFALSE\t0\tVISITOR_INFO1_LIVE\txyz\n",
        encoding="utf-8",
    )
    out_dir = tmp_path / "output"
    result = normalize_cookies_file(str(cookies), out_dir)
    assert result != str(cookies)
    fixed_content = Path(result).read_text(encoding="utf-8")
    assert fixed_content.startswith(".youtube.com\tTRUE\t")


def test_normalize_cookies_reuses_rotated_copy(tmp_path: Path) -> None:
    import os

    cookies = tmp_path / "cookies.txt"
    cookies.write_text(
        ".youtube.com\tFALSE\t/\tFALSE\t0\tVISITOR_INFO1_LIVE\txyz\n",
        encoding="utf-8",
    )
    out_dir = tmp_path / "output"
    result = Path(normalize_cookies_file(str(cookies), out_dir))
    # Simulate yt-dlp saving rotated cookie values back into the copy.
    result.write_text(
        ".youtube.com\tTRUE\t/\tFALSE\t0\tVISITOR_INFO1_LIVE\trotated\n",
        encoding="utf-8",
    )
    os.utime(result, (cookies.stat().st_mtime + 10, cookies.stat().st_mtime + 10))
    again = Path(normalize_cookies_file(str(cookies), out_dir))
    assert again == result
    assert "rotated" in again.read_text(encoding="utf-8")
    # A fresh export (original newer than the copy) regenerates the copy.
    os.utime(cookies, (result.stat().st_mtime + 10, result.stat().st_mtime + 10))
    fresh = Path(normalize_cookies_file(str(cookies), out_dir))
    assert "rotated" not in fresh.read_text(encoding="utf-8")


def test_normalize_cookies_none() -> None:
    assert normalize_cookies_file(None, Path("/tmp")) is None


def test_extract_wheel_dlls(tmp_path: Path) -> None:
    import zipfile

    wheel = tmp_path / "fake.whl"
    with zipfile.ZipFile(wheel, "w") as zf:
        zf.writestr("nvidia/cublas/bin/cublas64_12.dll", b"dll-bytes")
        zf.writestr("nvidia/cublas/bin/cublasLt64_12.dll", b"dll-bytes")
        zf.writestr("nvidia/cublas/include/cublas.h", b"header")
        zf.writestr("nvidia_cublas_cu12.dist-info/METADATA", b"meta")
    bin_dir = tmp_path / "bin"
    assert _extract_wheel_dlls(wheel, bin_dir) == 2
    assert (bin_dir / "cublas64_12.dll").read_bytes() == b"dll-bytes"
    assert not (bin_dir / "cublas.h").exists()


def test_ensure_cuda_runtime_non_windows() -> None:
    import sys

    if sys.platform != "win32":
        # On non-Windows platforms this is a no-op that must not download.
        assert ensure_cuda_runtime(lambda message: None) is True


def test_normalize_runtime_for_platform_forces_cpu_on_macos() -> None:
    device, compute_type, note = normalize_runtime_for_platform("cuda", "float16", platform="darwin")
    assert device == "cpu"
    assert compute_type == "int8"
    assert note and "CPU-only" in note


def test_normalize_runtime_for_platform_keeps_cuda_on_windows() -> None:
    device, compute_type, note = normalize_runtime_for_platform("cuda", "float16", platform="win32")
    assert device == "cuda"
    assert compute_type == "float16"
    assert note is None


def test_output_formats_constant() -> None:
    assert "txt" in OUTPUT_FORMATS
    assert "srt" in OUTPUT_FORMATS
    assert "vtt" in OUTPUT_FORMATS
    assert "json" in OUTPUT_FORMATS


class TestParseUrls:
    def test_single_url(self) -> None:
        urls = parse_urls("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        assert urls == ["https://www.youtube.com/watch?v=dQw4w9WgXcQ"]

    def test_multiple_urls_one_per_line(self) -> None:
        text = (
            "https://www.youtube.com/watch?v=aaaaaaaaaaa\n"
            "https://www.youtube.com/watch?v=bbbbbbbbbbb\n"
        )
        urls = parse_urls(text)
        assert len(urls) == 2

    def test_deduplicates(self) -> None:
        text = (
            "https://www.youtube.com/watch?v=aaaaaaaaaaa\n"
            "https://www.youtube.com/watch?v=aaaaaaaaaaa\n"
        )
        urls = parse_urls(text)
        assert len(urls) == 1

    def test_ignores_comments(self) -> None:
        text = (
            "# This is a comment\n"
            "https://www.youtube.com/watch?v=aaaaaaaaaaa\n"
        )
        urls = parse_urls(text)
        assert len(urls) == 1

    def test_ignores_blank_lines(self) -> None:
        text = "\n\nhttps://www.youtube.com/watch?v=aaaaaaaaaaa\n\n"
        urls = parse_urls(text)
        assert len(urls) == 1

    def test_short_url(self) -> None:
        urls = parse_urls("https://youtu.be/dQw4w9WgXcQ")
        assert len(urls) == 1

    def test_shorts_url(self) -> None:
        urls = parse_urls("https://www.youtube.com/shorts/aaaaaaaaaaa")
        assert len(urls) == 1

    def test_embed_url(self) -> None:
        urls = parse_urls("https://www.youtube.com/embed/dQw4w9WgXcQ")
        assert len(urls) == 1

    def test_live_url(self) -> None:
        urls = parse_urls("https://www.youtube.com/live/aaaaaaaaaaa")
        assert len(urls) == 1

    def test_mixed_text_with_urls(self) -> None:
        text = "Check this out https://www.youtube.com/watch?v=aaaaaaaaaaa and this https://youtu.be/bbbbbbbbbbb"
        urls = parse_urls(text)
        assert len(urls) == 2

    def test_empty_input(self) -> None:
        assert parse_urls("") == []
        assert parse_urls("# just comments\n") == []

    def test_plain_non_youtube_url(self) -> None:
        text = "https://example.com/video.mp4"
        urls = parse_urls(text)
        assert len(urls) == 1
        assert urls[0] == "https://example.com/video.mp4"

def test_purge_download_cache_removes_orphans(tmp_path: Path) -> None:
    cache = download_cache_dir(tmp_path)
    cache.mkdir(parents=True)
    (cache / "Video [abc123].mp3").write_bytes(b"audio")
    (cache / "Video [abc123].info.json").write_text("{}", encoding="utf-8")
    # a transcript in the real output dir must be left untouched
    keep = tmp_path / "Video.pt.md"
    keep.write_text("transcript", encoding="utf-8")

    removed = purge_download_cache(tmp_path)

    assert removed == 2
    assert list(cache.iterdir()) == []
    assert keep.exists()


def test_purge_download_cache_no_dir(tmp_path: Path) -> None:
    assert purge_download_cache(tmp_path) == 0


def test_safe_cuda_compute_type_forces_float16_on_blackwell(monkeypatch) -> None:
    import transcribe_youtube as ty

    # RTX 50-series reports compute_cap 12.0; int8 GEMM aborts there.
    monkeypatch.setattr(ty, "gpu_compute_cap", lambda: 12.0)
    chosen, note = ty.safe_cuda_compute_type("int8")
    assert chosen == "float16"
    assert note and "Blackwell" in note

    chosen, note = ty.safe_cuda_compute_type("int8_float16")
    assert chosen == "float16"


def test_safe_cuda_compute_type_keeps_int8_on_older_gpu(monkeypatch) -> None:
    import transcribe_youtube as ty

    # Ada (compute_cap 8.9) runs int8 fine, so leave it alone.
    monkeypatch.setattr(ty, "gpu_compute_cap", lambda: 8.9)
    chosen, note = ty.safe_cuda_compute_type("int8")
    assert chosen == "int8"
    assert note is None


def test_safe_cuda_compute_type_unknown_gpu_is_noop(monkeypatch) -> None:
    import transcribe_youtube as ty

    # No nvidia-smi / unreadable cap must not change the request.
    monkeypatch.setattr(ty, "gpu_compute_cap", lambda: None)
    chosen, note = ty.safe_cuda_compute_type("float16")
    assert chosen == "float16"
    assert note is None
