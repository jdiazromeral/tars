from tars import backup


def test_prune_keeps_the_newest_bundles(tmp_path):
    names = [f"tars-vault-2026100{d}-090000.bundle" for d in range(1, 5)]
    for name in names:
        (tmp_path / name).write_bytes(b"")
    (tmp_path / "unrelated.bundle").write_bytes(b"")
    removed = backup.prune(tmp_path, keep=2)
    assert [p.name for p in removed] == names[:2]
    assert sorted(p.name for p in tmp_path.iterdir()) == names[2:] + ["unrelated.bundle"]
