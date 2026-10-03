class AppError(Exception):
    def __init__(self, status: int, code: str, message: str, retryable: bool = False):
        """Store an API error's HTTP status, stable code, public message, and retryability."""
        self.status = status
        self.code = code
        self.message = message
        self.retryable = retryable
        super().__init__(message)
