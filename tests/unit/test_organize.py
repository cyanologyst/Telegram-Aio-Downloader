"""'Organize videos' must never surprise-delete or touch active downloads."""

from app.services.organize import apply_organize, plan_organize


def _write(path, data=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_plan_moves_video_and_matching_subtitles(tmp_path):
    _write(tmp_path / "[YTS] Movie (2020)" / "[YTS] Movie (2020).mkv")
    _write(tmp_path / "[YTS] Movie (2020)" / "[YTS] Movie (2020).en.srt")
    _write(tmp_path / "[YTS] Movie (2020)" / "info.nfo", b"12345")

    plan = plan_organize(tmp_path)

    assert plan.video_count == 1
    assert sorted(dst.name for _, dst in plan.moves) == [
        "[YTS] Movie (2020).en.srt",
        "[YTS] Movie (2020).mkv",
    ]
    assert (plan.leftover_files, plan.leftover_bytes) == (1, 5)
    # Planning alone changes nothing on disk.
    assert (tmp_path / "[YTS] Movie (2020)" / "[YTS] Movie (2020).mkv").exists()


def test_apply_keeps_folders_with_leftovers_by_default(tmp_path):
    _write(tmp_path / "Show" / "ep1.mp4")
    _write(tmp_path / "Show" / "cover.jpg")
    _write(tmp_path / "Clip" / "clip.mp4")

    result = apply_organize(plan_organize(tmp_path), delete_leftovers=False)

    assert (tmp_path / "ep1.mp4").exists() and (tmp_path / "clip.mp4").exists()
    assert (tmp_path / "Show" / "cover.jpg").exists()
    assert not (tmp_path / "Clip").exists()
    assert (result.moved, result.removed_folders, result.kept_folders) == (2, 1, 1)


def test_apply_can_delete_leftovers_when_asked(tmp_path):
    _write(tmp_path / "Show" / "ep1.mp4")
    _write(tmp_path / "Show" / "Sample" / "sample.txt")

    result = apply_organize(plan_organize(tmp_path), delete_leftovers=True)

    assert (tmp_path / "ep1.mp4").exists()
    assert not (tmp_path / "Show").exists()
    assert result.removed_folders == 1


def test_active_downloads_are_skipped(tmp_path):
    _write(tmp_path / "Aria" / "movie.mkv")
    _write(tmp_path / "Aria.aria2")  # aria2 control file next to the folder
    _write(tmp_path / "Ytdl" / "video.mp4")
    _write(tmp_path / "Ytdl" / "video2.mp4.part")
    _write(tmp_path / "Named" / "x.mp4")

    plan = plan_organize(tmp_path, busy_names={"Named"})

    assert plan.moves == []
    assert sorted(plan.skipped_busy) == ["Aria", "Named", "Ytdl"]


def test_protected_folders_and_parents_are_untouched(tmp_path):
    _write(tmp_path / "Adult" / "Site" / "v.mp4")
    _write(tmp_path / "Other" / "Inner" / "v.mp4")
    _write(tmp_path / "outside.mp4")

    plan = plan_organize(tmp_path / "Other")
    root_plan = plan_organize(tmp_path, protected_names={"Adult", "Other"})

    assert [dst for _, dst in plan.moves] == [tmp_path / "Other" / "v.mp4"]
    assert root_plan.moves == []


def test_name_clashes_get_a_suffix(tmp_path):
    _write(tmp_path / "video.mp4")
    _write(tmp_path / "A" / "video.mp4")
    _write(tmp_path / "B" / "video.mp4")

    plan = plan_organize(tmp_path)

    assert sorted(dst.name for _, dst in plan.moves) == ["video_1.mp4", "video_2.mp4"]
