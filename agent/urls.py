from django.urls import path
from . import views

urlpatterns = [
    path("", views.index, name="index"),
    path("api/upload/", views.upload_project, name="upload_project"),
    path("api/ask/", views.ask_question, name="ask_question"),
]
