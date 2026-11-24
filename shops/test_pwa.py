"""PWA: manifest, root-scoped service worker, and offline page."""
from django.test import TestCase
from django.urls import reverse


class PwaTests(TestCase):
    def test_manifest_is_installable(self):
        resp = self.client.get('/manifest.webmanifest')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Content-Type'], 'application/manifest+json')
        data = resp.json()
        self.assertEqual(data['display'], 'standalone')
        self.assertEqual(data['start_url'], '/?source=pwa')
        sizes = {(i['sizes'], i['purpose']) for i in data['icons']}
        self.assertIn(('192x192', 'any'), sizes)
        self.assertIn(('512x512', 'any'), sizes)
        self.assertIn(('512x512', 'maskable'), sizes)

    def test_service_worker_served_from_root(self):
        resp = self.client.get('/sw.js')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('application/javascript', resp['Content-Type'])
        self.assertEqual(resp['Service-Worker-Allowed'], '/')
        self.assertIn('no-cache', resp['Cache-Control'])
        body = resp.content.decode()
        self.assertIn('"/offline/"', body)
        self.assertIn('"/payment/"', body)          # payments are never intercepted
        self.assertNotIn('{{', body)

    def test_offline_page(self):
        resp = self.client.get(reverse('pwa_offline'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "You're offline")

    def test_pages_link_manifest_and_register_worker(self):
        page = self.client.get(reverse('home'))
        self.assertContains(page, 'rel="manifest" href="/manifest.webmanifest"')
        self.assertContains(page, "navigator.serviceWorker.register('/sw.js'")
