"""Small bounded Razorpay transport; creation requests are never retried.

No SDK dependency, secrets in URLs, redirects, or provider error bodies in API
responses. Call it from synchronous routes or a worker, never the event loop.
"""

import httpx


class RazorpayError(Exception):
    def __init__(self, *, uncertain=True):
        super().__init__("Payment provider request failed")
        self.uncertain = uncertain


class RazorpayClient:
    def __init__(self, settings):
        self.auth = (settings.razorpay_key_id, settings.razorpay_key_secret.get_secret_value())

    def request(self, method, path, *, params=None, json=None):
        try:
            with httpx.Client(
                timeout=httpx.Timeout(10.0, connect=5.0), follow_redirects=False
            ) as client:
                response = client.request(
                    method,
                    "https://api.razorpay.com/v1/" + path,
                    auth=self.auth,
                    params=params,
                    json=json,
                )
        except httpx.HTTPError:
            raise RazorpayError() from None
        if not 200 <= response.status_code < 300:
            raise RazorpayError(
                uncertain=response.status_code >= 500 or response.status_code == 429
            )
        try:
            result = response.json()
        except ValueError:
            raise RazorpayError() from None
        if not isinstance(result, dict):
            raise RazorpayError()
        return result

    def get(self, path, *, params=None):
        return self.request("GET", path, params=params)

    def post(self, path, data):
        return self.request("POST", path, json=data)

    def items(self, path, *, params=None):
        # 2,000 records is comfortably above a 120-cycle subscription. If the
        # bound is exceeded, fail closed rather than infer no matching record.
        for skip in range(0, 2000, 100):
            result = self.get(path, params={**(params or {}), "count": 100, "skip": skip})
            items = result.get("items")
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise RazorpayError()
            yield from items
            if len(items) < 100:
                return
        raise RazorpayError()
