"""Placeholder transport; the real implementation lands in Phase 1 Task M6."""


class GoogleGmailTransport:
    def __init__(self, *, api_endpoint: str, timeout: float = 30.0) -> None:
        self.api_endpoint = api_endpoint
        self.timeout = timeout

    def send_raw(
        self, *, email: str, access_token: str, raw_b64url: str, thread_id: str | None = None
    ) -> None:
        raise NotImplementedError("GoogleGmailTransport.send_raw is replaced in Task M6")
