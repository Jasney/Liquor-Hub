from django.core.management.base import BaseCommand, CommandError

from store.image_finder.matching import score_candidate, snapshot
from store.models import ImageCandidate, ImageSource


class Command(BaseCommand):
    help = "Refresh metadata scores after product edits; resets match approval and never changes rights or downloads images."

    def add_arguments(self, parser):
        parser.add_argument("candidate_id", type=int)

    def handle(self, *args, **options):
        try:
            candidate = ImageCandidate.objects.select_related("product__category").get(pk=options["candidate_id"])
        except ImageCandidate.DoesNotExist as error:
            raise CommandError("Candidate not found.") from error
        if candidate.published_at:
            raise CommandError("Withdraw the image before refreshing its match.")
        result = {"title": candidate.title, "attributes": candidate.metadata.get("attributes", {}),
                  "source_url": candidate.source_url, "width": candidate.width, "height": candidate.height}
        score, checks, variant, source = score_candidate(candidate.product, result, list(ImageSource.objects.filter(is_active=True)))
        candidate.confidence, candidate.match_checks, candidate.variant, candidate.source = score, checks, variant, source
        candidate.product_snapshot = snapshot(candidate.product)
        candidate.match_status, candidate.match_reviewer, candidate.match_reviewed_at = "pending", None, None
        candidate.save(update_fields=["confidence", "match_checks", "variant", "source", "product_snapshot", "match_status", "match_reviewer", "match_reviewed_at"])
        self.stdout.write("Checks refreshed. A new human match review is required; rights verification is unchanged.")
