"""Profile rules shared across areas."""
from jobfinder.models import Profile


def bump_version(profile: Profile) -> None:
    """The profile changed in a way scoring reads: matches and letters scored before are now stale."""
    profile.version += 1
