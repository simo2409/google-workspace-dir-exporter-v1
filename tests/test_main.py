import json
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

import main as m


# ---------------------------------------------------------------------------
# extract_folder_id
# ---------------------------------------------------------------------------


class TestExtractFolderId:
    def test_standard_folder_url(self):
        url = "https://drive.google.com/drive/folders/1abc123XYZ"
        assert m.extract_folder_id(url) == "1abc123XYZ"

    def test_folder_url_with_user_index(self):
        url = "https://drive.google.com/drive/u/0/folders/1abc-_XYZ"
        assert m.extract_folder_id(url) == "1abc-_XYZ"

    def test_folder_url_with_user_index_1(self):
        url = "https://drive.google.com/drive/u/1/folders/abc_123-XYZ"
        assert m.extract_folder_id(url) == "abc_123-XYZ"

    def test_folder_url_with_trailing_slash(self):
        url = "https://drive.google.com/drive/folders/folderID123/"
        assert m.extract_folder_id(url) == "folderID123"

    def test_folder_url_with_query_params(self):
        url = "https://drive.google.com/drive/folders/folderID?usp=sharing"
        assert m.extract_folder_id(url) == "folderID"

    def test_file_url_returns_none(self):
        url = "https://drive.google.com/file/d/1abc123XYZ/view"
        assert m.extract_folder_id(url) is None

    def test_unrecognised_url_returns_none(self):
        assert m.extract_folder_id("https://example.com/no-folder") is None

    def test_empty_string_returns_none(self):
        assert m.extract_folder_id("") is None


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------


def _write_cfg(tmp_path, data, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(data))
    monkeypatch.setattr(m, "CONFIG_FILE", cfg)


class TestLoadConfig:
    def test_valid_config_with_global_output(self, tmp_path, monkeypatch):
        data = {
            "output": "/tmp/out",
            "folders": [{"url": "https://drive.google.com/drive/folders/ABC"}],
        }
        _write_cfg(tmp_path, data, monkeypatch)
        config = m.load_config()
        assert config["output"] == "/tmp/out"
        assert config["folders"][0]["url"] == "https://drive.google.com/drive/folders/ABC"

    def test_valid_config_with_per_folder_output(self, tmp_path, monkeypatch):
        data = {
            "folders": [
                {"url": "https://drive.google.com/drive/folders/A", "output": "~/pathA/"},
                {"url": "https://drive.google.com/drive/folders/B", "output": "~/pathB/"},
            ]
        }
        _write_cfg(tmp_path, data, monkeypatch)
        config = m.load_config()
        assert config["folders"][0]["output"] == "~/pathA/"
        assert config["folders"][1]["output"] == "~/pathB/"

    def test_valid_config_mixed_output(self, tmp_path, monkeypatch):
        data = {
            "output": "~/default/",
            "folders": [
                {"url": "https://drive.google.com/drive/folders/A", "output": "~/custom/"},
                {"url": "https://drive.google.com/drive/folders/B"},
            ],
        }
        _write_cfg(tmp_path, data, monkeypatch)
        config = m.load_config()
        assert len(config["folders"]) == 2

    def test_missing_file_exits(self, tmp_path, monkeypatch):
        monkeypatch.setattr(m, "CONFIG_FILE", tmp_path / "nonexistent.json")
        with pytest.raises(SystemExit):
            m.load_config()

    def test_invalid_json_exits(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.json"
        cfg.write_text("not { valid } json")
        monkeypatch.setattr(m, "CONFIG_FILE", cfg)
        with pytest.raises(SystemExit):
            m.load_config()

    def test_missing_folders_key_exits(self, tmp_path, monkeypatch):
        _write_cfg(tmp_path, {"output": "/tmp/out"}, monkeypatch)
        with pytest.raises(SystemExit):
            m.load_config()

    def test_folders_not_list_exits(self, tmp_path, monkeypatch):
        _write_cfg(tmp_path, {"folders": "not-a-list"}, monkeypatch)
        with pytest.raises(SystemExit):
            m.load_config()

    def test_folder_entry_without_url_exits(self, tmp_path, monkeypatch):
        _write_cfg(tmp_path, {"folders": [{"output": "/tmp/out"}]}, monkeypatch)
        with pytest.raises(SystemExit):
            m.load_config()

    def test_folder_entry_as_plain_string_exits(self, tmp_path, monkeypatch):
        _write_cfg(tmp_path, {"folders": ["https://plain-string-url"]}, monkeypatch)
        with pytest.raises(SystemExit):
            m.load_config()


# ---------------------------------------------------------------------------
# load_metadata / save_metadata
# ---------------------------------------------------------------------------


class TestMetadata:
    def test_load_missing_returns_empty_dict(self, tmp_path):
        assert m.load_metadata(tmp_path) == {}

    def test_roundtrip(self, tmp_path):
        data = {
            "file-id": {
                "filename": "doc.docx",
                "modifiedTime": "2026-01-01T00:00:00Z",
                "md5": None,
            }
        }
        m.save_metadata(tmp_path, data)
        assert m.load_metadata(tmp_path) == data

    def test_file_lives_in_base_dir(self, tmp_path):
        m.save_metadata(tmp_path, {"k": "v"})
        assert (tmp_path / "_metadata.json").exists()

    def test_save_overwrites_previous(self, tmp_path):
        m.save_metadata(tmp_path, {"a": 1})
        m.save_metadata(tmp_path, {"b": 2})
        assert m.load_metadata(tmp_path) == {"b": 2}


# ---------------------------------------------------------------------------
# list_folder_items
# ---------------------------------------------------------------------------


class TestListFolderItems:
    def _make_service(self, pages: list[list[dict]]) -> MagicMock:
        svc = MagicMock()
        responses = []
        for i, page in enumerate(pages):
            resp = {"files": page}
            if i < len(pages) - 1:
                resp["nextPageToken"] = f"token_{i}"
            responses.append(resp)
        svc.files.return_value.list.return_value.execute.side_effect = responses
        return svc

    def test_single_page(self):
        items = [{"id": "1", "name": "file.pdf", "mimeType": "application/pdf"}]
        svc = self._make_service([items])
        result = m.list_folder_items(svc, "folder123")
        assert result == items

    def test_multiple_pages(self):
        page1 = [{"id": "1", "name": "a.pdf", "mimeType": "application/pdf"}]
        page2 = [{"id": "2", "name": "b.pdf", "mimeType": "application/pdf"}]
        svc = self._make_service([page1, page2])
        result = m.list_folder_items(svc, "folder123")
        assert len(result) == 2
        assert result[0]["id"] == "1"
        assert result[1]["id"] == "2"

    def test_empty_folder(self):
        svc = self._make_service([[]])
        result = m.list_folder_items(svc, "folder123")
        assert result == []

    def test_query_filters_by_folder_and_not_trashed(self):
        svc = self._make_service([[]])
        m.list_folder_items(svc, "myFolder")
        call_kwargs = svc.files.return_value.list.call_args.kwargs
        assert "'myFolder' in parents" in call_kwargs["q"]
        assert "trashed = false" in call_kwargs["q"]


# ---------------------------------------------------------------------------
# download_file_item — skip logic
# ---------------------------------------------------------------------------


def _make_file_meta(
    file_id: str = "file123",
    name: str = "TestDoc",
    mime_type: str = "application/pdf",
    modified_time: str = "2026-01-01T00:00:00Z",
    md5: str | None = "abc123",
) -> dict:
    meta = {
        "id": file_id,
        "name": name,
        "mimeType": mime_type,
        "modifiedTime": modified_time,
    }
    if md5 is not None:
        meta["md5Checksum"] = md5
    return meta


def _make_stats() -> dict:
    return {"downloaded": 0, "skipped": 0, "updated": 0, "errors": 0}


class TestDownloadFileItem:
    NATIVE_MIME = "application/vnd.google-apps.document"
    PDF_MIME = "application/pdf"
    MOD_TIME = "2026-01-01T00:00:00Z"
    FILE_ID = "file123"

    # --- native files ---

    def test_skip_native_when_modified_time_matches(self, tmp_path):
        svc = MagicMock()
        meta = _make_file_meta(mime_type=self.NATIVE_MIME, md5=None)
        metadata = {self.FILE_ID: {"modifiedTime": self.MOD_TIME, "md5": None}}
        stats = _make_stats()
        m.download_file_item(svc, meta, tmp_path, tmp_path, metadata, stats)
        svc.files.return_value.export_media.assert_not_called()
        assert stats["skipped"] == 1

    def test_skip_native_cross_day(self, tmp_path):
        svc = MagicMock()
        meta = _make_file_meta(mime_type=self.NATIVE_MIME, md5=None)
        metadata = {self.FILE_ID: {"modifiedTime": self.MOD_TIME, "md5": None}}
        stats = _make_stats()
        dest = tmp_path / "2026-05-02"
        dest.mkdir()
        m.download_file_item(svc, meta, dest, tmp_path, metadata, stats)
        svc.files.return_value.export_media.assert_not_called()
        assert stats["skipped"] == 1

    def test_download_native_first_time(self, tmp_path):
        svc = MagicMock()
        meta = _make_file_meta(mime_type=self.NATIVE_MIME, md5=None)
        mock_dl = MagicMock()
        mock_dl.next_chunk.return_value = (MagicMock(progress=lambda: 1.0), True)
        stats = _make_stats()
        with patch("main.MediaIoBaseDownload", return_value=mock_dl):
            m.download_file_item(svc, meta, tmp_path, tmp_path, {}, stats)
        svc.files.return_value.export_media.assert_called_once()
        assert stats["downloaded"] == 1

    def test_update_native_when_modified_time_changed(self, tmp_path):
        svc = MagicMock()
        meta = _make_file_meta(mime_type=self.NATIVE_MIME, modified_time="2026-02-01T00:00:00Z", md5=None)
        metadata = {self.FILE_ID: {"modifiedTime": self.MOD_TIME, "md5": None}}
        mock_dl = MagicMock()
        mock_dl.next_chunk.return_value = (MagicMock(progress=lambda: 1.0), True)
        stats = _make_stats()
        with patch("main.MediaIoBaseDownload", return_value=mock_dl):
            m.download_file_item(svc, meta, tmp_path, tmp_path, metadata, stats)
        assert metadata[self.FILE_ID]["modifiedTime"] == "2026-02-01T00:00:00Z"
        assert stats["updated"] == 1

    # --- non-native files ---

    def test_skip_non_native_when_md5_matches(self, tmp_path):
        svc = MagicMock()
        meta = _make_file_meta(md5="abc123")
        metadata = {self.FILE_ID: {"md5": "abc123"}}
        stats = _make_stats()
        m.download_file_item(svc, meta, tmp_path, tmp_path, metadata, stats)
        svc.files.return_value.get_media.assert_not_called()
        assert stats["skipped"] == 1

    def test_download_non_native_when_md5_changed(self, tmp_path):
        svc = MagicMock()
        meta = _make_file_meta(md5="newmd5")
        metadata = {self.FILE_ID: {"md5": "oldmd5"}}
        mock_dl = MagicMock()
        mock_dl.next_chunk.return_value = (MagicMock(progress=lambda: 1.0), True)
        stats = _make_stats()
        with patch("main.MediaIoBaseDownload", return_value=mock_dl):
            m.download_file_item(svc, meta, tmp_path, tmp_path, metadata, stats)
        assert metadata[self.FILE_ID]["md5"] == "newmd5"
        assert stats["updated"] == 1

    def test_download_non_native_when_drive_has_no_md5(self, tmp_path):
        svc = MagicMock()
        meta = _make_file_meta(md5=None)
        metadata = {self.FILE_ID: {"md5": None}}
        mock_dl = MagicMock()
        mock_dl.next_chunk.return_value = (MagicMock(progress=lambda: 1.0), True)
        stats = _make_stats()
        with patch("main.MediaIoBaseDownload", return_value=mock_dl):
            m.download_file_item(svc, meta, tmp_path, tmp_path, metadata, stats)
        svc.files.return_value.get_media.assert_called_once()

    def test_skip_unsupported_native_type(self, tmp_path):
        svc = MagicMock()
        meta = _make_file_meta(mime_type="application/vnd.google-apps.unknown", md5=None)
        stats = _make_stats()
        m.download_file_item(svc, meta, tmp_path, tmp_path, {}, stats)
        assert self.FILE_ID not in {}
        assert stats["skipped"] == 1

    def test_metadata_keyed_by_file_id(self, tmp_path):
        svc = MagicMock()
        meta = _make_file_meta(file_id="unique-id-42", md5=None, mime_type=self.NATIVE_MIME)
        mock_dl = MagicMock()
        mock_dl.next_chunk.return_value = (MagicMock(progress=lambda: 1.0), True)
        metadata: dict = {}
        stats = _make_stats()
        with patch("main.MediaIoBaseDownload", return_value=mock_dl):
            m.download_file_item(svc, meta, tmp_path, tmp_path, metadata, stats)
        assert "unique-id-42" in metadata


# ---------------------------------------------------------------------------
# download_folder_recursive
# ---------------------------------------------------------------------------


class TestDownloadFolderRecursive:
    def _make_service_with_items(self, items_by_folder: dict) -> MagicMock:
        svc = MagicMock()

        def list_execute():
            # Use side_effect on list().execute to return different items per call
            pass

        call_count = [0]
        folder_ids = list(items_by_folder.keys())

        def make_list_execute(folder_id):
            items = items_by_folder.get(folder_id, [])
            resp = MagicMock()
            resp.execute.return_value = {"files": items}
            return resp

        # We'll set up the mock differently using a closure
        original_list = svc.files.return_value.list

        def list_side_effect(**kwargs):
            q = kwargs.get("q", "")
            for fid in folder_ids:
                if fid in q:
                    return make_list_execute(fid)
            result = MagicMock()
            result.execute.return_value = {"files": []}
            return result

        svc.files.return_value.list.side_effect = list_side_effect
        return svc

    def test_downloads_files_in_flat_folder(self, tmp_path):
        items = [
            {"id": "f1", "name": "report.pdf", "mimeType": "application/pdf", "md5Checksum": "abc", "modifiedTime": "2026-01-01T00:00:00Z"},
        ]
        svc = self._make_service_with_items({"folder1": items})
        mock_dl = MagicMock()
        mock_dl.next_chunk.return_value = (MagicMock(progress=lambda: 1.0), True)
        stats = _make_stats()
        with patch("main.MediaIoBaseDownload", return_value=mock_dl):
            m.download_folder_recursive(svc, "folder1", tmp_path, tmp_path, {}, stats)
        assert (tmp_path / "report.pdf").exists()

    def test_creates_subfolder_and_recurses(self, tmp_path):
        subfolder_item = {
            "id": "subfolder1",
            "name": "SubDir",
            "mimeType": "application/vnd.google-apps.folder",
        }
        file_in_sub = {
            "id": "f2",
            "name": "nested.pdf",
            "mimeType": "application/pdf",
            "md5Checksum": "xyz",
            "modifiedTime": "2026-01-01T00:00:00Z",
        }
        svc = self._make_service_with_items({
            "rootfolder": [subfolder_item],
            "subfolder1": [file_in_sub],
        })
        mock_dl = MagicMock()
        mock_dl.next_chunk.return_value = (MagicMock(progress=lambda: 1.0), True)
        stats = _make_stats()
        with patch("main.MediaIoBaseDownload", return_value=mock_dl):
            m.download_folder_recursive(svc, "rootfolder", tmp_path, tmp_path, {}, stats)
        assert (tmp_path / "SubDir").is_dir()
        assert (tmp_path / "SubDir" / "nested.pdf").exists()

    def test_saves_metadata_after_each_file(self, tmp_path):
        items = [
            {"id": "f1", "name": "a.pdf", "mimeType": "application/pdf", "md5Checksum": "a1", "modifiedTime": "T"},
            {"id": "f2", "name": "b.pdf", "mimeType": "application/pdf", "md5Checksum": "b1", "modifiedTime": "T"},
        ]
        svc = self._make_service_with_items({"root": items})
        mock_dl = MagicMock()
        mock_dl.next_chunk.return_value = (MagicMock(progress=lambda: 1.0), True)
        stats = _make_stats()
        save_calls = []
        original_save = m.save_metadata

        def spy_save(base_dir, metadata):
            save_calls.append(len(metadata))
            original_save(base_dir, metadata)

        with patch("main.MediaIoBaseDownload", return_value=mock_dl):
            with patch("main.save_metadata", side_effect=spy_save):
                m.download_folder_recursive(svc, "root", tmp_path, tmp_path, {}, stats)

        # save_metadata called once per file
        assert len(save_calls) == 2

    def test_skips_already_downloaded_files(self, tmp_path):
        items = [
            {"id": "f1", "name": "a.pdf", "mimeType": "application/pdf", "md5Checksum": "abc", "modifiedTime": "T"},
        ]
        svc = self._make_service_with_items({"root": items})
        metadata = {"f1": {"md5": "abc"}}
        stats = _make_stats()
        with patch("main.save_metadata"):
            m.download_folder_recursive(svc, "root", tmp_path, tmp_path, metadata, stats)
        assert stats["skipped"] == 1
        svc.files.return_value.get_media.assert_not_called()
