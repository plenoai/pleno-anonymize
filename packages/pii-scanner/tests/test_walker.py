from pathlib import Path

from pleno_pii_scanner.walker import walk, is_binary


def test_walk_skips_noise_dirs(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("x = 1\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "ignored.js").write_text("var y = 1;")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[core]\n")

    out = sorted(p.name for p in walk(tmp_path))
    assert out == ["main.py"]


def test_walk_respects_gitignore(tmp_path: Path):
    (tmp_path / ".gitignore").write_text("secrets.txt\n")
    (tmp_path / "secrets.txt").write_text("hidden")
    (tmp_path / "code.txt").write_text("visible")
    out = {p.name for p in walk(tmp_path)}
    assert "secrets.txt" not in out
    assert "code.txt" in out


def test_walk_skips_binary(tmp_path: Path):
    (tmp_path / "img.bin").write_bytes(b"\x00\x01\x02\x03text\x00")
    (tmp_path / "ok.txt").write_text("hello")
    out = sorted(p.name for p in walk(tmp_path))
    assert out == ["ok.txt"]


def test_walk_respects_size_limit(tmp_path: Path):
    (tmp_path / "big.txt").write_text("a" * 200)
    (tmp_path / "small.txt").write_text("ok")
    out = sorted(p.name for p in walk(tmp_path, max_file_size=100))
    assert out == ["small.txt"]


def test_is_binary(tmp_path: Path):
    p = tmp_path / "b.bin"
    p.write_bytes(b"\x00abc")
    assert is_binary(p) is True
    p2 = tmp_path / "t.txt"
    p2.write_text("hello")
    assert is_binary(p2) is False
