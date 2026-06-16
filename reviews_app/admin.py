from django.contrib import admin

from reviews_app.models import Review


@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = ('restaurant', 'overall_rating', 'resolution_status', 'created_at')
