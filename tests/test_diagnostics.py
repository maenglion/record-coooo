import concurrent.futures
import os
import tempfile
import threading
import unittest
from unittest import mock

import yt_dlp
import bot_discord as app


class DiagnosticsTests(unittest.TestCase):
    def test_youtube_player_error_not_automatically_called_login_block(self):
        msg, retry = app.friendly_error_message('ERROR: [youtube] Failed to extract any player response')
        self.assertTrue(retry)
        self.assertIn('아직 확인할 수 없습니다', msg)
        self.assertNotIn('로그인을 요구', msg)

    def test_diagnostic_flags_are_safe_and_specific(self):
        diag = app.DownloadDiagnostics()
        with yt_dlp.YoutubeDL({'quiet': True, 'logger': diag}) as ydl:
            ydl.report_warning('Unable to download API page: HTTP Error 403: Forbidden')
            ydl.report_warning('The provided YouTube account cookies are no longer valid')
        self.assertEqual(diag.flags, {'http_403', 'cookie_invalid'})
        msg, retry = app.friendly_error_message('player response', diag.flags)
        self.assertIn('쿠키가 유효하지 않다', msg)
        self.assertFalse(retry)
        msg, _ = app.friendly_error_message('player response', {'http_403'})
        self.assertIn('403', msg)
        self.assertNotIn('로그인을 요구', msg)

    def test_concurrent_downloads_use_private_copies_and_keep_warnings(self):
        barrier = threading.Barrier(2)
        used_paths = []
        mutex = threading.Lock()

        class FakeYDL:
            def __init__(self, opts):
                self.opts = opts

            def __enter__(self):
                return self

            def __exit__(self, *args):
                with open(self.opts['cookiefile'], 'w', encoding='utf-8') as f:
                    f.write('changed by yt-dlp')

            def extract_info(self, url, download):
                cookie = self.opts['cookiefile']
                with mutex:
                    used_paths.append(cookie)
                barrier.wait(timeout=5)
                with open(cookie, encoding='utf-8') as f:
                    assert f.read() == 'original test cookie'
                self.opts['logger'].warning('HTTP Error 403: Forbidden')
                raise yt_dlp.utils.DownloadError('Failed to extract any player response')

        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, 'source_cookies.txt')
            with open(source, 'w', encoding='utf-8') as f:
                f.write('original test cookie')
            with mock.patch.object(app, 'COOKIE_FILE', source), \
                 mock.patch.object(app, 'get_ffmpeg_path', return_value='ffmpeg'), \
                 mock.patch.object(app.yt_dlp, 'YoutubeDL', FakeYDL):
                def run(i):
                    try:
                        app.download_media(f'https://example.test/{i}', directory)
                    except yt_dlp.utils.DownloadError as e:
                        return e.diagnostic_flags
                    self.fail('Expected DownloadError')

                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    flags = list(pool.map(run, range(2)))
            self.assertEqual(flags, [frozenset({'http_403'})] * 2)
            self.assertEqual(len(set(used_paths)), 2)
            self.assertTrue(all(not os.path.exists(path) for path in used_paths))
            with open(source, encoding='utf-8') as f:
                self.assertEqual(f.read(), 'original test cookie')


if __name__ == '__main__':
    unittest.main()
