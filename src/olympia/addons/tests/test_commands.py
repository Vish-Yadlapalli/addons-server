import random
from contextlib import contextmanager
from datetime import timedelta

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError

from freezegun import freeze_time
from unittest import mock
import pytest

from olympia import amo
from olympia.addons.management.commands import (
    fix_langpacks_with_max_version_star,
    process_addons,
)
from olympia.addons.models import Addon, DeniedGuid
from olympia.abuse.models import AbuseReport
from olympia.amo.tests import (
    TestCase,
    addon_factory,
    user_factory,
    version_factory,
)
from olympia.applications.models import AppVersion
from olympia.blocklist.models import Block
from olympia.files.models import FileValidation
from olympia.ratings.models import Rating, RatingAggregate
from olympia.reviewers.models import AutoApprovalSummary
from olympia.users.models import UserProfile
from olympia.versions.models import ApplicationsVersions, Version, VersionPreview


def test_process_addons_invalid_task():
    with pytest.raises(CommandError):
        call_command('process_addons', task='foo')


@contextmanager
def count_subtask_calls(original_function):
    """Mock a celery tasks subtask method and record it's calls.

    You can't mock a celery task `.subtask` method if that task is used
    inside a chord or group unfortunately because of some type checking
    that is use inside Celery 4+.

    So this wraps the original method and restores it and records the calls
    on it's own.
    """
    original_function_subtask = original_function.subtask
    called = []

    def _subtask_wrapper(*args, **kwargs):
        called.append({'args': args, 'kwargs': kwargs})
        return original_function_subtask(*args, **kwargs)

    original_function.subtask = _subtask_wrapper

    yield called

    original_function.subtask = original_function_subtask


@freeze_time('2019-04-01')
@pytest.mark.django_db
def test_process_addons_limit_addons():
    user_factory(id=settings.TASK_USER_ID)
    addon_ids = [addon_factory(status=amo.STATUS_APPROVED).id for _ in range(5)]
    assert Addon.objects.count() == 5

    with count_subtask_calls(process_addons.sign_addons) as calls:
        call_command('process_addons', task='resign_addons_for_cose')
        assert len(calls) == 1
        assert calls[0]['kwargs']['args'] == [addon_ids]

    with count_subtask_calls(process_addons.sign_addons) as calls:
        call_command('process_addons', task='resign_addons_for_cose', limit=2)
        assert len(calls) == 1
        assert calls[0]['kwargs']['args'] == [addon_ids[:2]]


@pytest.mark.django_db
@mock.patch.object(process_addons.Command, 'get_pks')
def test_process_addons_batch_size(mock_get_pks):
    addon_ids = [random.randrange(1000) for _ in range(101)]
    mock_get_pks.return_value = addon_ids

    with count_subtask_calls(process_addons.recreate_previews) as calls:
        call_command('process_addons', task='recreate_previews')
        assert len(calls) == 2
        assert calls[0]['kwargs']['args'] == [addon_ids[:100]]
        assert calls[1]['kwargs']['args'] == [addon_ids[100:]]

    with count_subtask_calls(process_addons.recreate_previews) as calls:
        call_command('process_addons', task='recreate_previews', **{'batch_size': 50})
        assert len(calls) == 3
        assert calls[0]['kwargs']['args'] == [addon_ids[:50]]
        assert calls[1]['kwargs']['args'] == [addon_ids[50:100]]
        assert calls[2]['kwargs']['args'] == [addon_ids[100:]]


class RecalculateWeightTestCase(TestCase):
    def test_only_affects_auto_approved_and_unconfirmed(self):
        # Non auto-approved add-on, should not be considered.
        addon_factory()

        # Non auto-approved add-on that has an AutoApprovalSummary entry,
        # should not be considered.
        AutoApprovalSummary.objects.create(
            version=addon_factory().current_version, verdict=amo.NOT_AUTO_APPROVED
        )

        # Add-on with the current version not auto-approved, should not be
        # considered.
        extra_addon = addon_factory()
        AutoApprovalSummary.objects.create(
            version=extra_addon.current_version, verdict=amo.AUTO_APPROVED
        )
        extra_addon.current_version.update(created=self.days_ago(1))
        version_factory(addon=extra_addon)

        # Add-on that was auto-approved but already confirmed, should not be
        # considered, it's too late.
        already_confirmed_addon = addon_factory()
        AutoApprovalSummary.objects.create(
            version=already_confirmed_addon.current_version,
            verdict=amo.AUTO_APPROVED,
            confirmed=True,
        )

        # Add-on that should be considered because it's current version is
        # auto-approved.
        auto_approved_addon = addon_factory()
        AutoApprovalSummary.objects.create(
            version=auto_approved_addon.current_version, verdict=amo.AUTO_APPROVED
        )
        # Add some extra versions that should not have an impact.
        version_factory(
            addon=auto_approved_addon, file_kw={'status': amo.STATUS_AWAITING_REVIEW}
        )
        version_factory(addon=auto_approved_addon, channel=amo.RELEASE_CHANNEL_UNLISTED)

        with count_subtask_calls(
            process_addons.recalculate_post_review_weight
        ) as calls:
            call_command('process_addons', task='recalculate_post_review_weight')

        assert len(calls) == 1
        assert calls[0]['kwargs']['args'] == [[auto_approved_addon.pk]]

    def test_task_works_correctly(self):
        addon = addon_factory(average_daily_users=100000)
        FileValidation.objects.create(file=addon.current_version.file, validation='{}')
        addon = Addon.objects.get(pk=addon.pk)
        summary = AutoApprovalSummary.objects.create(
            version=addon.current_version, verdict=amo.AUTO_APPROVED
        )
        assert summary.weight == 0

        call_command('process_addons', task='recalculate_post_review_weight')

        summary.reload()
        # Weight should be 10 because of average_daily_users / 10000.
        assert summary.weight == 10


class ConstantlyRecalculateWeightTestCase(TestCase):
    def test_affects_correct_addons(self):
        # *not considered* - Non auto-approved add-on
        addon_factory()

        # *not considered* - Non auto-approved add-on that has an
        # AutoApprovalSummary entry
        AutoApprovalSummary.objects.create(
            version=addon_factory().current_version, verdict=amo.NOT_AUTO_APPROVED
        )

        # *not considered* -Add-on with the current version not auto-approved
        extra_addon = addon_factory()
        AutoApprovalSummary.objects.create(
            version=extra_addon.current_version, verdict=amo.AUTO_APPROVED
        )
        extra_addon.current_version.update(created=self.days_ago(1))
        version_factory(addon=extra_addon)

        # *not considered* - current version is auto-approved but doesn't
        # have recent abuse reports or low ratings
        auto_approved_addon = addon_factory()
        AutoApprovalSummary.objects.create(
            version=auto_approved_addon.current_version, verdict=amo.AUTO_APPROVED
        )

        # *considered* - current version is auto-approved and
        # has a recent rating with rating <= 3
        auto_approved_addon1 = addon_factory()
        summary = AutoApprovalSummary.objects.create(
            version=auto_approved_addon1.current_version, verdict=amo.AUTO_APPROVED
        )
        Rating.objects.create(
            created=summary.modified + timedelta(days=3),
            addon=auto_approved_addon1,
            version=auto_approved_addon1.current_version,
            rating=2,
            body='Apocalypse',
            user=user_factory(),
        ),

        # *not considered* - current version is auto-approved but
        # has a recent rating with rating > 3
        auto_approved_addon2 = addon_factory()
        summary = AutoApprovalSummary.objects.create(
            version=auto_approved_addon2.current_version, verdict=amo.AUTO_APPROVED
        )
        Rating.objects.create(
            created=summary.modified + timedelta(days=3),
            addon=auto_approved_addon2,
            version=auto_approved_addon2.current_version,
            rating=4,
            body='Apocalypse',
            user=user_factory(),
        ),

        # *not considered* - current version is auto-approved but
        # has a recent rating with rating > 3
        auto_approved_addon3 = addon_factory()
        summary = AutoApprovalSummary.objects.create(
            version=auto_approved_addon3.current_version, verdict=amo.AUTO_APPROVED
        )
        Rating.objects.create(
            created=summary.modified + timedelta(days=3),
            addon=auto_approved_addon3,
            version=auto_approved_addon3.current_version,
            rating=4,
            body='Apocalypse',
            user=user_factory(),
        ),

        # *not considered* - current version is auto-approved but
        # has a low rating that isn't recent enough
        auto_approved_addon4 = addon_factory()
        summary = AutoApprovalSummary.objects.create(
            version=auto_approved_addon4.current_version, verdict=amo.AUTO_APPROVED
        )
        Rating.objects.create(
            created=summary.modified - timedelta(days=3),
            addon=auto_approved_addon4,
            version=auto_approved_addon4.current_version,
            rating=1,
            body='Apocalypse',
            user=user_factory(),
        ),

        # *considered* - current version is auto-approved and
        # has a recent abuse report
        auto_approved_addon5 = addon_factory()
        summary = AutoApprovalSummary.objects.create(
            version=auto_approved_addon5.current_version, verdict=amo.AUTO_APPROVED
        )
        AbuseReport.objects.create(
            addon=auto_approved_addon5, created=summary.modified + timedelta(days=3)
        )

        # *not considered* - current version is auto-approved but
        # has an abuse report that isn't recent enough
        auto_approved_addon6 = addon_factory()
        summary = AutoApprovalSummary.objects.create(
            version=auto_approved_addon6.current_version, verdict=amo.AUTO_APPROVED
        )
        AbuseReport.objects.create(
            addon=auto_approved_addon6, created=summary.modified - timedelta(days=3)
        )

        # *considered* - current version is auto-approved and
        # has an abuse report through it's author that is recent enough
        author = user_factory()
        auto_approved_addon7 = addon_factory(users=[author])
        summary = AutoApprovalSummary.objects.create(
            version=auto_approved_addon7.current_version, verdict=amo.AUTO_APPROVED
        )
        AbuseReport.objects.create(
            user=author, created=summary.modified + timedelta(days=3)
        )

        # *not considered* - current version is auto-approved and
        # has an abuse report through it's author that is recent enough
        # BUT the abuse report is deleted.
        author = user_factory()
        auto_approved_addon8 = addon_factory(users=[author])
        summary = AutoApprovalSummary.objects.create(
            version=auto_approved_addon8.current_version, verdict=amo.AUTO_APPROVED
        )
        AbuseReport.objects.create(
            user=author,
            state=AbuseReport.STATES.DELETED,
            created=summary.modified + timedelta(days=3),
        )

        # *not considered* - current version is auto-approved and
        # has a recent rating with rating <= 3
        # but the rating is deleted.
        auto_approved_addon9 = addon_factory()
        summary = AutoApprovalSummary.objects.create(
            version=auto_approved_addon9.current_version, verdict=amo.AUTO_APPROVED
        )
        Rating.objects.create(
            created=summary.modified + timedelta(days=3),
            addon=auto_approved_addon9,
            version=auto_approved_addon9.current_version,
            deleted=True,
            rating=2,
            body='Apocalypse',
            user=user_factory(),
        ),

        # *considered* - current version is auto-approved and
        # has an abuse report through it's author that is recent enough
        # Used to test that we only recalculate the weight for
        # the most recent version
        author = user_factory()
        auto_approved_addon8 = addon_factory(
            users=[author], version_kw={'version': '0.1'}
        )

        AutoApprovalSummary.objects.create(
            version=auto_approved_addon8.current_version, verdict=amo.AUTO_APPROVED
        )

        # Let's create a new `current_version` and summary
        current_version = version_factory(addon=auto_approved_addon8, version='0.2')

        summary = AutoApprovalSummary.objects.create(
            version=current_version, verdict=amo.AUTO_APPROVED
        )

        AbuseReport.objects.create(
            user=author, created=summary.modified + timedelta(days=3)
        )

        mod = 'olympia.reviewers.tasks.AutoApprovalSummary.calculate_weight'
        with mock.patch(mod) as calc_weight_mock:
            with count_subtask_calls(
                process_addons.recalculate_post_review_weight
            ) as calls:
                call_command(
                    'process_addons', task='constantly_recalculate_post_review_weight'
                )

        assert len(calls) == 1
        assert calls[0]['kwargs']['args'] == [
            [
                auto_approved_addon1.pk,
                auto_approved_addon5.pk,
                auto_approved_addon7.pk,
                auto_approved_addon8.pk,
            ]
        ]

        # Only 4 calls for each add-on, doesn't consider the extra version
        # that got created for addon 8
        assert calc_weight_mock.call_count == 4


class TestExtractColorsFromStaticThemes(TestCase):
    @mock.patch('olympia.addons.tasks.extract_colors_from_image')
    def test_basic(self, extract_colors_from_image_mock):
        addon = addon_factory(type=amo.ADDON_STATICTHEME)
        preview = VersionPreview.objects.create(version=addon.current_version)
        extract_colors_from_image_mock.return_value = [
            {'h': 4, 's': 8, 'l': 15, 'ratio': 0.16}
        ]
        call_command('process_addons', task='extract_colors_from_static_themes')
        preview.reload()
        assert preview.colors == [{'h': 4, 's': 8, 'l': 15, 'ratio': 0.16}]


class TestResignAddonsForCose(TestCase):
    @mock.patch('olympia.lib.crypto.tasks.sign_file')
    def test_basic(self, sign_file_mock):
        file_kw = {'filename': 'webextension.xpi'}
        user_factory(id=settings.TASK_USER_ID)

        with freeze_time('2019-04-01'):
            addon_with_history = addon_factory(file_kw=file_kw)
            # Create a few more versions for this add-on to test that we only
            # re-sign current versions
            version_factory(addon=addon_with_history, file_kw=file_kw)
            version_factory(addon=addon_with_history, file_kw=file_kw)
            version_factory(addon=addon_with_history, file_kw=file_kw)

            addon_factory(file_kw=file_kw)
            addon_factory(type=amo.ADDON_STATICTHEME, file_kw=file_kw)
            addon_factory(type=amo.ADDON_LPAPP, file_kw=file_kw)
            addon_factory(type=amo.ADDON_DICT, file_kw=file_kw)

        # Don't resign add-ons created after April 4th 2019
        with freeze_time('2019-05-01'):
            addon_factory(file_kw=file_kw)
            addon_factory(type=amo.ADDON_STATICTHEME, file_kw=file_kw)

        # Search add-ons won't get re-signed, same with deleted and disabled
        # versions. Also, only public addons are being resigned
        addon_factory(status=amo.STATUS_DISABLED, file_kw=file_kw)
        addon_factory(status=amo.STATUS_AWAITING_REVIEW, file_kw=file_kw)
        addon_factory(status=amo.STATUS_NULL, file_kw=file_kw)

        call_command('process_addons', task='resign_addons_for_cose')

        assert sign_file_mock.call_count == 5


class TestDeleteObsoleteAddons(TestCase):
    def setUp(self):
        # Some add-ons that shouldn't be deleted
        self.extension = addon_factory()
        self.static_theme = addon_factory(type=amo.ADDON_STATICTHEME)
        self.dictionary = addon_factory(type=amo.ADDON_DICT)
        # And some obsolete ones
        self.xul_theme = addon_factory(type=2)  # _ADDON_THEME
        self.lpaddon = addon_factory()
        self.lpaddon.update(type=amo.ADDON_LPADDON)
        self.plugin = addon_factory()
        self.plugin.update(type=amo.ADDON_PLUGIN)
        self.lwt = addon_factory(type=9, guid=None)  # _ADDON_PERSONA
        self.webapp = addon_factory()
        self.webapp.update(type=11)  # webapp

        assert Addon.unfiltered.count() == 8

    def test_hard(self):
        # add it already to check that the conflict is ignored
        DeniedGuid.objects.create(guid=self.xul_theme.guid)
        DeniedGuid.objects.all().count() == 1

        call_command('process_addons', task='delete_obsolete_addons', with_deleted=True)

        assert Addon.unfiltered.count() == 3
        assert Addon.unfiltered.get(id=self.extension.id)
        assert Addon.unfiltered.get(id=self.static_theme.id)
        assert Addon.unfiltered.get(id=self.dictionary.id)

        # check the guids have been added to DeniedGuid
        DeniedGuid.objects.all().count() == 4
        assert DeniedGuid.objects.get(guid=self.xul_theme.guid)
        assert DeniedGuid.objects.get(guid=self.lpaddon.guid)
        assert DeniedGuid.objects.get(guid=self.plugin.guid)
        # no self.lwt as it didn't have a guid
        assert DeniedGuid.objects.get(guid=self.webapp.guid)

    def test_hard_with_already_deleted(self):
        Addon.unfiltered.update(status=amo.STATUS_DELETED)
        self.test_hard()

    def test_normal(self):
        call_command('process_addons', task='delete_obsolete_addons')

        assert Addon.unfiltered.count() == 8
        assert Addon.objects.count() == 3
        assert Addon.objects.get(id=self.extension.id)
        assert Addon.objects.get(id=self.static_theme.id)
        assert Addon.objects.get(id=self.dictionary.id)


class TestFixLangpacksWithMaxVersionStar(TestCase):
    def setUp(self):
        addon = addon_factory(  # Should autocreate the AppVersions for Firefox
            type=amo.ADDON_LPAPP,
            version_kw={
                'min_app_version': '77.0',
                'max_app_version': '*',
            },
        )
        # Add the missing AppVersions for Android, and assign them to the addon
        min_android = AppVersion.objects.get_or_create(
            application=amo.ANDROID.id, version='77.0'
        )[0]
        max_android_star = AppVersion.objects.get_or_create(
            application=amo.ANDROID.id, version='*'
        )[0]
        ApplicationsVersions.objects.create(
            application=amo.ANDROID.id,
            version=addon.current_version,
            min=min_android,
            max=max_android_star,
        )

        addon_factory(  # Same kind of langpack, but without android compat.
            type=amo.ADDON_LPAPP,
            version_kw={
                'min_app_version': '77.0',
                'max_app_version': '*',
            },
        )
        addon = addon_factory(  # Shouldn't be touched, its max is not '*'.
            type=amo.ADDON_LPAPP,
            version_kw={
                'min_app_version': '77.0',
                'max_app_version': '77.*',
            },
        )
        max_android = AppVersion.objects.get_or_create(
            application=amo.ANDROID.id, version='77.*'
        )[0]
        ApplicationsVersions.objects.create(
            application=amo.ANDROID.id,
            version=addon.current_version,
            min=min_android,
            max=max_android,
        )

    def test_find_affected_langpacks(self):
        command = fix_langpacks_with_max_version_star.Command()
        qs = command.find_affected_langpacks()
        assert qs.count() == 2

    def test_basic(self):
        call_command('fix_langpacks_with_max_version_star')
        # Each versions should still have valid ApplicationsVersions, and they
        # should all point to 77.* - all '*' have been converted.
        for version in Version.objects.all():
            for app in version.compatible_apps:
                assert version.compatible_apps[app].max.version == '77.*'


def test_update_rating_aggregates():
    addon = addon_factory()
    Rating.objects.create(addon=addon, user=user_factory(), rating=4)
    assert RatingAggregate.objects.filter(addon=addon).delete()
    call_command('process_addons', task='update_rating_aggregates')
    assert RatingAggregate.objects.filter(addon=addon).exists()
    addon = Addon.objects.get(id=addon.id)
    assert addon.ratingaggregate
    assert addon.ratingaggregate.count_4 == 1


@pytest.mark.django_db
def test_delete_list_theme_previews():
    old_list_preview_size = {
        'thumbnail': [529, 64],
        'image': [760, 92],
    }
    addon = addon_factory(type=amo.ADDON_STATICTHEME)
    other_version = version_factory(
        addon=addon, file_kw={'status': amo.STATUS_AWAITING_REVIEW}
    )

    firefox_preview = VersionPreview.objects.create(
        version=addon.current_version, sizes=amo.THEME_PREVIEW_RENDERINGS['firefox']
    )
    amo_preview = VersionPreview.objects.create(
        version=addon.current_version, sizes=amo.THEME_PREVIEW_RENDERINGS['amo']
    )
    old_list_preview = VersionPreview.objects.create(
        version=addon.current_version, sizes=old_list_preview_size
    )
    other_firefox_preview = VersionPreview.objects.create(
        version=other_version, sizes=amo.THEME_PREVIEW_RENDERINGS['firefox']
    )
    other_amo_preview = VersionPreview.objects.create(
        version=other_version, sizes=amo.THEME_PREVIEW_RENDERINGS['amo']
    )
    other_old_list_preview = VersionPreview.objects.create(
        version=other_version, sizes=old_list_preview_size
    )

    call_command('process_addons', task='delete_list_theme_previews')

    assert VersionPreview.objects.count() == 4
    assert VersionPreview.objects.filter(id=firefox_preview.id).exists()
    assert VersionPreview.objects.filter(id=amo_preview.id).exists()
    assert not VersionPreview.objects.filter(id=old_list_preview.id).exists()
    assert VersionPreview.objects.filter(id=other_firefox_preview.id).exists()
    assert VersionPreview.objects.filter(id=other_amo_preview.id).exists()
    assert not VersionPreview.objects.filter(id=other_old_list_preview.id).exists()


@pytest.mark.django_db
def test_disable_blocked_addons():
    UserProfile.objects.create(pk=settings.TASK_USER_ID)
    user = user_factory()
    incomplete = addon_factory(status=amo.STATUS_NULL)
    Block.objects.create(guid=incomplete.guid, updated_by=user)
    nominated = addon_factory(status=amo.STATUS_NOMINATED)
    Block.objects.create(guid=nominated.guid, updated_by=user)
    approved = addon_factory(status=amo.STATUS_APPROVED)
    Block.objects.create(guid=approved.guid, updated_by=user)
    deleted = addon_factory(status=amo.STATUS_DELETED)
    Block.objects.create(guid=deleted.guid, updated_by=user)
    only_partially_blocked = addon_factory(status=amo.STATUS_APPROVED)
    Block.objects.create(
        guid=only_partially_blocked.guid, min_version='1', updated_by=user
    )
    addon_factory()  # just a random addon
    assert Addon.unfiltered.filter(status=amo.STATUS_DISABLED).count() == 0

    call_command('process_addons', task='disable_blocked_addons')

    # these should have changed
    assert incomplete.reload().status == amo.STATUS_DISABLED
    assert nominated.reload().status == amo.STATUS_DISABLED
    assert approved.reload().status == amo.STATUS_DISABLED
    # these shouldn't have been changed
    assert deleted.reload().status == amo.STATUS_DELETED
    assert only_partially_blocked.reload().status == amo.STATUS_APPROVED

    assert Addon.unfiltered.filter(status=amo.STATUS_DISABLED).count() == 3
