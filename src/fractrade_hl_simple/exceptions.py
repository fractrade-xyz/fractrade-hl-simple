class HyperliquidException(Exception):
    """Base class for exceptions in this module."""
    pass

class ConfigurationException(HyperliquidException):
    """Exception raised for configuration errors."""
    pass

class UnauthorizedException(HyperliquidException):
    """Exception raised for 401 Unauthorized status code."""
    def __init__(self, message="UNAUTHORIZED"):
        super().__init__(message)

class RateLimitException(HyperliquidException):
    """Exception raised for rate limit exceeded."""
    def __init__(self, message="RATE_LIMIT_EXCEEDED"):
        super().__init__(message)

class ServerErrorException(HyperliquidException):
    """Exception raised for 500 status code."""
    def __init__(self, message="INTERNAL_ERROR"):
        super().__init__(message)

class OrderException(HyperliquidException):
    """Base for order-related errors."""
    pass

class InsufficientMarginException(OrderException):
    """Raised when there is not enough margin for the order."""
    def __init__(self, message="Insufficient margin to place order"):
        super().__init__(message)

class OrderNotFoundException(OrderException):
    """Raised when an order is not found."""
    def __init__(self, message="Order not found"):
        super().__init__(message)

class PositionNotFoundException(HyperliquidException):
    """Raised when a position is not found."""
    def __init__(self, message="Position not found"):
        super().__init__(message)
