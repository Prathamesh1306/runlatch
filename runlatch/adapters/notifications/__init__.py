"""Notification provider adapters and router for runlatch."""

from runlatch.adapters.notifications.discord import DiscordNotificationProvider
from runlatch.adapters.notifications.email import SMTPEmailNotificationProvider
from runlatch.adapters.notifications.github_comment import (
    GitHubCommentNotificationProvider,
)
from runlatch.adapters.notifications.router import (
    NotificationRouter,
    build_signed_review_url,
)
from runlatch.adapters.notifications.slack import SlackNotificationProvider
from runlatch.adapters.notifications.webhook import WebhookNotificationProvider

__all__ = [
    "DiscordNotificationProvider",
    "GitHubCommentNotificationProvider",
    "NotificationRouter",
    "SMTPEmailNotificationProvider",
    "SlackNotificationProvider",
    "WebhookNotificationProvider",
    "build_signed_review_url",
]
