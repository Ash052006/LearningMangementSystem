"""URLs for the accounts app."""

from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("signup/", views.SignUpView.as_view(), name="signup"),
    path("login/", views.LoginView.as_view(), name="login"),
    # Django 5's LogoutView accepts POST only, which matches our POST-only
    # state-change rule; it redirects to settings.LOGOUT_REDIRECT_URL.
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("dashboard/", views.DashboardView.as_view(), name="dashboard"),
]
