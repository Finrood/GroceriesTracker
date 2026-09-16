from axes.helpers import get_client_ip_address
from axes.models import AccessAttempt
from django.conf import settings
from django.contrib.auth.models import User
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse


def get_ip(request):
    return get_client_ip_address(request)


class SecurityConfigurationTests(SimpleTestCase):
    def test_username_lockout_configuration(self):
        self.assertEqual(settings.AXES_LOCKOUT_PARAMETERS, ['username'])
        self.assertEqual(settings.AXES_IPWARE_META_PRECEDENCE_ORDER, ('REMOTE_ADDR',))
        self.assertEqual(settings.AXES_FAILURE_LIMIT, 5)
        self.assertEqual(settings.AXES_COOLOFF_TIME.total_seconds(), 900)
        self.assertEqual(settings.AXES_HTTP_RESPONSE_CODE, 429)
        self.assertEqual(settings.AUTHENTICATION_BACKENDS[0], 'axes.backends.AxesStandaloneBackend')
        self.assertEqual(settings.MIDDLEWARE[-1], 'axes.middleware.AxesMiddleware')

    def test_sqlite_immediate_without_request_transactions(self):
        database = settings.DATABASES['default']
        self.assertEqual(database['OPTIONS']['transaction_mode'], 'IMMEDIATE')
        self.assertFalse(database.get('ATOMIC_REQUESTS', False))

    def test_scrape_views_have_no_outer_atomic(self):
        import inspect
        from tracker import views
        self.assertNotIn('@transaction.atomic', inspect.getsource(views.process_nfce_url))
        self.assertNotIn('@transaction.atomic', inspect.getsource(views.confirm_refresh))
        self.assertIn('with transaction.atomic():', inspect.getsource(views.confirm_refresh))

    def test_csp_report_only(self):
        self.assertFalse(settings.SECURE_CSP)
        self.assertEqual(settings.SECURE_CSP_REPORT_ONLY['default-src'], ["'self'"])
        self.assertEqual(settings.SECURE_CSP_REPORT_ONLY['object-src'], ["'none'"])
        self.assertIn('django.middleware.csp.ContentSecurityPolicyMiddleware', settings.MIDDLEWARE)

    def test_forged_forwarding_headers_are_not_trusted(self):
        request = RequestFactory().post(
            '/accounts/login/',
            HTTP_X_FORWARDED_FOR='1.2.3.4',
            HTTP_X_REAL_IP='5.6.7.8',
            REMOTE_ADDR='203.0.113.9',
        )
        self.assertEqual(get_ip(request), '203.0.113.9')


@override_settings(SECURE_SSL_REDIRECT=False)
class UsernameLockoutTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username='lockout', password='correct-horse')
        self.login_url = reverse('login')

    def post_login(self, password):
        return self.client.post(self.login_url, {'username': 'lockout', 'password': password})

    def test_locks_out_after_limit(self):
        for _ in range(settings.AXES_FAILURE_LIMIT - 1):
            response = self.post_login('wrong')
            self.assertEqual(response.status_code, 200)
        response = self.post_login('wrong')
        self.assertEqual(response.status_code, 429)
        response = self.post_login('correct-horse')
        self.assertEqual(response.status_code, 429)

    def test_resets_on_success(self):
        self.post_login('wrong')
        self.client.post(self.login_url, {'username': 'lockout', 'password': 'correct-horse'})
        self.assertEqual(AccessAttempt.objects.count(), 0)
