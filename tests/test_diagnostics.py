import concurrent.futures
import os
import tempfile
import threading
import unittest
from unittest import mock

import yt_dlp
import bot_discord as app


class DiagnosticsTests(unittest.TestCase):
    def test_youtube_probe_compares_options_without_disclosing_errors(self):
        captured = []
        secret = 'SENSITIVE_VALUE_MUST_NOT_APPEAR'

        class FakeYDL:
            def __init__(self, opts):
                self.opts = opts
                captured.append(opts)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def extract_info(self, url, download):
                self.opts['logger'].warning('HTTP Error 403: Forbidden')
                raise yt_dlp.utils.DownloadError('Failed to extract any player response ' + secret)

        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, 'cookies.txt')
            with open(source, 'w', encoding='utf-8') as f:
                f.write('test cookie')
            with mock.patch.object(app, 'COOKIE_FILE', source), \
                 mock.patch.object(app.yt_dlp, 'YoutubeDL', FakeYDL):
                results = [app.youtube_diagnostic_probe(m) for m in ('current', 'default_cookie', 'anonymous')]
        self.assertEqual(captured[0]['extractor_args']['youtube']['player_client'], ['web', 'web_safari'])
        self.assertNotIn('extractor_args', captured[1])
        self.assertNotIn('cookiefile', captured[2])
        self.assertTrue(all('HTTP 403' in result for result in results))
        self.assertTrue(all(secret not in result for result in results))
        self.assertTrue(all(o['skip_download'] and o['retries'] == 0 for o in captured))

    def test_youtube_cookie_domain_status_does_not_claim_cookie_validity(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, 'cookies.txt')
            with open(source, 'w', encoding='utf-8') as f:
                f.write('# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsecret\n')
            with mock.patch.object(app, 'COOKIE_FILE', source):
                result = app.youtube_cookie_status()
        self.assertIn('도메인 쿠키 있음', result)
        self.assertIn('유효 여부 미확인', result)
        self.assertNotIn('secret', result)

    def test_platform_url_filter(self):
        examples = [
            'https://www.facebook.com/share/v/1eBuvP5VrN/',
            'https://fb.watch/example/',
            'https://www.threads.com/share/BAcTPWwMby/',
            'https://www.instagram.com/reel/DfZXTucyqJI/',
            'https://x.com/example/status/123',
        ]
        for url in examples:
            with self.subTest(url=url):
                self.assertEqual(app.first_supported_url('영상 부탁해요 ' + url), url)
        for url in ('https://facebook.com.evil.example/v/123',
                    'https://youtube.com@evil.example/watch?v=1',
                    'https://example.com/?url=facebook.com',
                    'http://facebook.com/share/v/abc',
                    'https://facebook.com:8080/share/v/abc'):
            with self.subTest(url=url):
                self.assertIsNone(app.first_supported_url(url))

    def test_unverified_threads_share_is_rejected(self):
        class FakeYDL:
            def __init__(self, opts):
                self.opts = opts

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def extract_info(self, url, download):
                self.opts['logger'].warning('Could not determine which post this link points to; using the first video on the page.')
                return {'id': 'unrelated-video'}

        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(app, 'COOKIE_FILE', os.path.join(directory, 'missing')), \
             mock.patch.object(app.yt_dlp, 'YoutubeDL', FakeYDL):
            with self.assertRaisesRegex(yt_dlp.utils.DownloadError, 'Could not verify which post'):
                app.download_media('https://threads.com/share/example/', directory)

    def test_threads_no_video_does_not_claim_block_or_no_video_for_sure(self):
        msg, retry = app.friendly_error_message(
            'ERROR: [threads] Post "DfmjHzvAe1W" has no downloadable video (text-only or unsupported)'
        )
        self.assertIn('찾지 못했어요', msg)
        self.assertIn('수 있습니다', msg)
        self.assertFalse(retry)

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
