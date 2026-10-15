from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone
from django_q.models import Schedule


MAINTENANCE_TASK = 'tracker.tasks.maintenance_requeue_enrichment'
MAINTENANCE_SCHEDULE = 'GroceriesTracker daily maintenance'


class Command(BaseCommand):
    help = 'Ensure the recurring maintenance schedule exists for the worker.'

    def handle(self, *args, **options):
        schedule = Schedule.objects.filter(name=MAINTENANCE_SCHEDULE).first()
        if schedule is None:
            # Adopt an existing out-of-band schedule for the same task rather
            # than creating a second daily requeue loop.
            schedule = Schedule.objects.filter(func=MAINTENANCE_TASK).order_by('id').first()

        if schedule is None:
            schedule = Schedule(
                name=MAINTENANCE_SCHEDULE,
                func=MAINTENANCE_TASK,
                schedule_type=Schedule.DAILY,
                repeats=-1,
                next_run=timezone.now() + timedelta(minutes=5),
            )
        else:
            schedule.name = MAINTENANCE_SCHEDULE
            schedule.func = MAINTENANCE_TASK
            schedule.schedule_type = Schedule.DAILY
            schedule.repeats = -1
            schedule.args = ''
            schedule.kwargs = ''
            if not schedule.next_run or schedule.next_run <= timezone.now():
                schedule.next_run = timezone.now() + timedelta(minutes=5)

        schedule.full_clean()
        schedule.save()
        Schedule.objects.filter(func=MAINTENANCE_TASK).exclude(pk=schedule.pk).delete()
        self.stdout.write(self.style.SUCCESS('Daily maintenance schedule is configured.'))
