# Generated by Django 2.2.12 on 2020-06-24 18:09

from django.db import migrations, models
import django.utils.timezone
import olympia.hero.models


class Migration(migrations.Migration):

    dependencies = [
        ('hero', '0010_auto_20200623_1949'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='primaryheroimage',
            options={'base_manager_name': 'objects', 'get_latest_by': 'created'},
        ),
        migrations.AddField(
            model_name='primaryheroimage',
            name='created',
            field=models.DateTimeField(blank=True, default=django.utils.timezone.now, editable=False),
        ),
        migrations.AddField(
            model_name='primaryheroimage',
            name='modified',
            field=models.DateTimeField(auto_now=True),
        ),
        migrations.AlterField(
            model_name='primaryheroimage',
            name='custom_image',
            field=models.ImageField(upload_to='hero-featured-image/', verbose_name='custom image path'),
        ),
    ]
