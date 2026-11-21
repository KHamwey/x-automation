"""Errors that mean the owner must re-authorize LinkedIn."""

from __future__ import annotations


class LinkedInAuthError(RuntimeError):
    """The LinkedIn access token is missing, expired, or was rejected.

    Personal tokens last about 60 days. Many apps do not receive a refresh
    token, so the fix is a local ``linkedin auth`` run and a secret update.
    """

    def banner(self) -> str:
        detail = str(self).strip() or "LinkedIn rejected the access token."
        return (
            "================================================================\n"
            "LINKEDIN AUTH FAILURE\n"
            f"{detail}\n"
            "\n"
            "Personal LinkedIn access tokens last about 60 days. Refresh tokens\n"
            "are returned only when LinkedIn has granted them to the app. If this\n"
            "token has no refresh_token, publishing stops until you re-authorize.\n"
            "\n"
            "Re-authorize on a machine with a browser:\n"
            "  python -m x_automation.cli linkedin auth\n"
            "\n"
            "Update the GitHub Actions secret (do not commit the token file):\n"
            "  gh secret set LINKEDIN_TOKEN_JSON < data/linkedin_token.json\n"
            "\n"
            "If a refresh token was granted, also set LINKEDIN_CLIENT_ID and\n"
            "LINKEDIN_CLIENT_SECRET so later runs can refresh before expiry.\n"
            "\n"
            "Then re-run the \"LinkedIn autopost\" workflow.\n"
            "================================================================"
        )
