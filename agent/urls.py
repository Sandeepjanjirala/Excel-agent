from django.urls import path
from . import views

urlpatterns = [
    path("", views.index, name="index"),
    path("api/upload/", views.upload_project, name="upload_project"),
    path("api/ask/", views.ask_question, name="ask_question"),
    path("api/project/delete/", views.delete_project, name="delete_project"),
    path("api/media/stats/", views.storage_stats_view, name="storage_stats"),
]
