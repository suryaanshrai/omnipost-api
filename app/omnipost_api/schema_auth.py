"""How the sign-in methods appear in the OpenAPI schema."""

from drf_spectacular.extensions import OpenApiAuthenticationExtension


class PersonalTokenScheme(OpenApiAuthenticationExtension):
    target_class = "omnipost_api.authentication.PersonalTokenAuthentication"
    name = "apiToken"

    def get_security_definition(self, auto_schema):
        return {
            "type": "http",
            "scheme": "bearer",
            "description": "A personal API token (op_...), from Settings -> API tokens.",
        }


class CookieTokenScheme(OpenApiAuthenticationExtension):
    target_class = "omnipost_api.authentication.CookieTokenAuthentication"
    name = "omnipostCookie"

    def get_security_definition(self, auto_schema):
        return {
            "type": "apiKey",
            "in": "cookie",
            "name": "omnipost_auth",
            "description": "The web app's sign-in cookie. Requests must also send X-OmniPost-Client.",
        }
