import dataclasses
import datetime
import dateutil.parser
import enum
import functools
import itertools
import os
import typing

import dacite
import yaml

import paths

own_dir = os.path.abspath(os.path.dirname(__file__))
repo_root = os.path.abspath(os.path.join(
    own_dir, os.path.pardir, os.path.pardir))


class FeatureType(enum.Enum):
    '''
    gardenlinux feature types as used in `features/*/info.yaml`

    Each gardenlinux flavour MUST specify exactly one platform and MAY
    specify an arbitrary amount of modifiers.
    '''
    PLATFORM = 'platform'
    MODIFIER = 'modifier'


Platform = str # see `features/*/info.yaml` / platforms() for allowed values
Modifier = str # see `features/*/info.yaml` / modifiers() for allowed values


@dataclasses.dataclass(frozen=True)
class Features:
    '''
    a FeatureDescriptor's feature cfg (currently, references to other features, only)
    '''
    include: typing.Tuple[Modifier] = tuple()


@dataclasses.dataclass(frozen=True)
class FeatureDescriptor:
    '''
    A gardenlinux feature descriptor (parsed from $repo_root/features/*/info.yaml)
    '''
    type: FeatureType
    name: str
    description: str = 'no description available'
    features: Features = None

    def included_feature_names(self) -> typing.Tuple[Modifier]:
        '''
        returns the tuple of feature names immediately depended-on by this feature
        '''
        if not self.features:
            return ()
        return self.features.include

    def included_features(self,
                          transitive=True
                          ) -> typing.Generator['FeatureDescriptor', None, None]:
        '''
        returns the tuple of features (transtively) included by this feature
        '''
        included_features = (feature_by_name(name)
                             for name in self.included_feature_names())

        for included_feature in included_features:
            if transitive:
                yield from included_feature.included_features()
            yield included_feature


class Architecture(enum.Enum):
    '''
    gardenlinux' target architectures, following Debian's naming
    '''
    AMD64 = 'amd64'


@dataclasses.dataclass(frozen=True)
class GardenlinuxFlavour:
    '''
    A specific flavour of gardenlinux.
    '''
    architecture: Architecture
    platform: str
    modifiers: typing.Tuple[Modifier]

    def calculate_modifiers(self):
        platform = feature_by_name(self.platform)
        yield from platform.included_features()
        yield from (
            feature_by_name(f) for f
            in normalised_modifiers(platform=self.platform, modifiers=self.modifiers)
        )

    def canonical_name_prefix(self):
        a = self.architecture.value
        fname_prefix = self.filename_prefix()

        return f'{a}/{fname_prefix}'

    def filename_prefix(self):
        p = self.platform
        m = '_'.join(sorted([m for m in self.modifiers]))

        return f'{p}-{m}'

    def __post_init__(self):
        # validate platform and modifiers
        platform_names = {platform.name for platform in platforms()}
        if not self.platform in platform_names:
            raise ValueError(
                f'unknown platform: {self.platform}. known: {platform_names}'
            )

        modifier_names = {modifier.name for modifier in modifiers()}
        unknown_mods = set(self.modifiers) - modifier_names
        if unknown_mods:
            raise ValueError(
                f'unknown modifiers: {unknown_mods}. known: {modifier_names}'
            )


@dataclasses.dataclass(frozen=True)
class GardenlinuxFlavourCombination:
    '''
    A declaration of a set of gardenlinux flavours. Deserialised from `flavours.yaml`.

    We intend to build a two-digit number of gardenlinux flavours (combinations
    of different architectures, platforms, and modifiers). To avoid tedious and redundant
    manual configuration, flavourset combinations are declared. Subsequently, the
    cross product of said combinations are generated.
    '''
    architectures: typing.Tuple[Architecture]
    platforms: typing.Tuple[Platform]
    modifiers: typing.Tuple[typing.Tuple[Modifier]]


@dataclasses.dataclass(frozen=True)
class GardenlinuxFlavourSet:
    '''
    A set of gardenlinux flavours
    '''
    name: str
    flavour_combinations: typing.Tuple[GardenlinuxFlavourCombination]

    def flavours(self):
        for comb in self.flavour_combinations:
            for arch, platf, mods in itertools.product(
                comb.architectures,
                comb.platforms,
                comb.modifiers,
            ):
                yield GardenlinuxFlavour(
                    architecture=arch,
                    platform=platf,
                    modifiers=normalised_modifiers(
                        platform=platf, modifiers=mods),
                )


@dataclasses.dataclass(frozen=True)
class ReleaseFile:
    '''
    base class for release-files
    '''
    name: str
    suffix: str


@dataclasses.dataclass(frozen=True)
class S3_ReleaseFile(ReleaseFile):
    '''
    A single build result file that was (or will be) uploaded to build result persistency store
    (S3).
    '''
    s3_key: str
    s3_bucket_name: str


@dataclasses.dataclass(frozen=True)
class ReleaseIdentifier:
    '''
    a partial ReleaseManifest with all attributes required to unambiguosly identify a
    release.
    '''
    build_committish: str
    version: str
    gardenlinux_epoch: int
    architecture: Architecture
    platform: Platform
    modifiers: typing.Tuple[Modifier]

    def flavour(self, normalise=True) -> GardenlinuxFlavour:
        mods = normalised_modifiers(
            platform=self.platform, modifiers=self.modifiers)

        return GardenlinuxFlavour(
            architecture=self.architecture,
            platform=self.platform,
            modifiers=mods,
        )

    def canonical_release_manifest_key_suffix(self):
        '''
        returns the canonical release manifest key. This key is used as a means to
        unambiguously identify it, and to thus be able to calculate its name if checking
        whether or not the given gardenlinux flavour has already been built and published.

        the key consists of:

        <canonical flavour name>-<version>

        where <canonical flavour name> is calculated from canonicalised_features()
        and <version> is either <gardenlinux-epoch>-<commit-hash[:6]>, or the release
        version (for releases).

        note that the full key should be prefixed (e.g. with manifest_key_prefix)
        '''
        flavour_name = '-'.join((
            f.name for f in canonicalised_features(
                platform=self.platform,
                modifiers=self.modifiers,
            )
        ))
        return f'{flavour_name}-{self.version}'

    def canonical_release_manifest_key(self):
        return f'{self.manifest_key_prefix}/{self.canonical_release_manifest_key_suffix()}'

    # attrs below are _transient_ (no typehint) and thus exempted from x-serialisation
    # treat as "static final"
    manifest_key_prefix = 'meta/singles'


class PublishedImageBase:
    pass


@dataclasses.dataclass(frozen=True)
class AwsPublishedImage:
    ami_id: str
    aws_region_id: str
    image_name: str


@dataclasses.dataclass(frozen=True)
class AwsPublishedImageSet(PublishedImageBase):
    published_aws_images: typing.Tuple[AwsPublishedImage]
    # release_identifier: typing.Optional[ReleaseIdentifier]


@dataclasses.dataclass(frozen=True)
class AlicloudPublishedImage:
    image_id: str
    region_id: str
    image_name: str


@dataclasses.dataclass(frozen=True)
class AlicloudPublishedImageSet(PublishedImageBase):
    published_alicloud_images: typing.Tuple[AlicloudPublishedImage]


@dataclasses.dataclass(frozen=True)
class GcpPublishedImage(PublishedImageBase):
    gcp_image_name: str
    gcp_project_name: str


@dataclasses.dataclass(frozen=True)
class ReleaseManifest(ReleaseIdentifier):
    '''
    metadata for a gardenlinux release variant that can be (or was) published to a persistency
    store, such as an S3 bucket.
    '''
    build_timestamp: str
    paths: typing.Tuple[typing.Union[S3_ReleaseFile]]
    published_image_metadata: typing.Union[AlicloudPublishedImageSet,
                                           AwsPublishedImageSet, GcpPublishedImage, None]

    def path_by_suffix(self, suffix: str):
        for path in self.paths:
            if path.suffix == suffix:
                return path
        else:
            raise ValueError(f'no path with {suffix=}')

    def release_identifier(self) -> ReleaseIdentifier:
        return ReleaseIdentifier(
            build_committish=self.build_committish,
            version=self.version,
            gardenlinux_epoch=self.gardenlinux_epoch,
            architecture=self.architecture,
            platform=self.platform,
            modifiers=self.modifiers,
        )


def normalised_modifiers(platform: Platform, modifiers) -> typing.Tuple[str]:
    '''
    determines the transitive closure of all features from the given platform and modifiers,
    and returns the (ASCII-upper-case-sorted) result as a `tuple` of str of all modifiers,
    except for the platform
    '''
    platform = feature_by_name(platform)
    modifiers = {feature_by_name(f) for f in modifiers}

    all_modifiers = set((m.name for m in modifiers))
    for m in modifiers:
        all_modifiers |= set((m.name for m in m.included_features()))

    for f in platform.included_features():
        all_modifiers.add(f.name)

    normalised_features = tuple(sorted(all_modifiers, key=str.upper))

    return normalised_features


def normalised_release_identifier(release_identifier: ReleaseIdentifier):
    modifiers = normalised_modifiers(
        platform=release_identifier.platform,
        modifiers=release_identifier.modifiers,
    )

    return dataclasses.replace(release_identifier, modifiers=modifiers)


def canonicalised_features(platform: Platform, modifiers) -> typing.Tuple[FeatureDescriptor]:
    '''
    calculates the "canonical" (/minimal) tuple of features required to unambiguosly identify
    a gardenlinux flavour. The result is returned as a (ASCII-upper-case-sorted) tuple of
    `FeatureDescriptor`, including the platform (which is always the first element).

    The minimal featureset is determined by removing all transitive dependencies (which are thus
    implied by the retained features).
    '''
    platform = feature_by_name(platform)
    minimal_modifiers = set((feature_by_name(m) for m in modifiers))

    # rm all transitive dependencies from platform
    minimal_modifiers -= set((platform.included_features(), *modifiers))

    # rm all transitive dependencies from modifiers
    for modifier in (feature_by_name(m) for m in modifiers):
        minimal_modifiers -= set(modifier.included_features())

    # canonical name: <platform>-<ordered-features> (UPPER-cased-sort, so _ is after alpha)
    minimal_modifiers = sorted(minimal_modifiers, key=lambda m: m.name.upper())

    return tuple((platform, *minimal_modifiers))


@dataclasses.dataclass(frozen=True)
class OnlineReleaseManifest(ReleaseManifest):
    '''
    a `ReleaseManifest` that was uploaded to a S3 bucket
    '''
    # injected iff retrieved from s3 bucket
    s3_key: str
    s3_bucket: str

    def stripped_manifest(self):
        raw = dataclasses.asdict(self)
        del raw['s3_key']
        del raw['s3_bucket']

        return ReleaseManifest(**raw)


@dataclasses.dataclass(frozen=True)
class ReleaseManifestSet:
    manifests: typing.Tuple[OnlineReleaseManifest]
    flavour_set_name: str

    # treat as static final
    release_manifest_set_prefix = 'meta/sets'


class PipelineFlavour(enum.Enum):
    SNAPSHOT = 'snapshot'
    RELEASE = 'release'


@dataclasses.dataclass(frozen=True)
class BuildCfg:
    aws_cfg_name: str
    aws_region: str
    s3_bucket_name: str
    gcp_bucket_name: str
    gcp_cfg_name: str
    storage_account_config_name: str
    service_principal_name: str
    plan_config_name: str
    oss_bucket_name: str
    alicloud_region: str
    alicloud_cfg_name: str


@dataclasses.dataclass(frozen=True)
class AzurePublishCfg:
    offer_id: str
    publisher_id: str
    plan_id: str
    service_principal_cfg_name: str  # references secret in cicd cluster
    storage_account_cfg_name: str


@dataclasses.dataclass(frozen=True)
class PublishCfg:
    azure: AzurePublishCfg


@dataclasses.dataclass(frozen=True)
class CicdCfg:
    name: str
    build: BuildCfg
    publish: PublishCfg


epoch_date = datetime.datetime.fromisoformat('2020-04-01')


def gardenlinux_epoch(date: typing.Union[str, datetime.datetime] = None):
    '''
    calculates the gardenlinux epoch for the given date (the amount of days since 2020-04-01)
    @param date: date (defaults to today); if str, must be compliant to iso-8601
    '''
    if date is None:
        date = datetime.datetime.today()
    elif isinstance(date, str):
        date = dateutil.parser.isoparse(date)

    if not isinstance(date, datetime.datetime):
        raise ValueError(date)

    gardenlinux_epoch = (date - epoch_date).days + 1

    if gardenlinux_epoch < 1:
        raise ValueError()  # must not be older than gardenlinux' inception
    return gardenlinux_epoch


_gl_epoch = gardenlinux_epoch  # alias for usage in snapshot_date


def snapshot_date(gardenlinux_epoch: int = None):
    '''
    calculates the debian snapshot repository timestamp from the given gardenlinux epoch in the
    format that is expected for said snapshot repository.
    @param gardenlinux_epoch: int, the gardenlinux epoch
    '''
    if gardenlinux_epoch is None:
        gardenlinux_epoch = _gl_epoch()
    gardenlinux_epoch = int(gardenlinux_epoch)
    if gardenlinux_epoch < 1:
        raise ValueError(gardenlinux_epoch)

    time_d = datetime.timedelta(days=gardenlinux_epoch - 1)

    date_str = (epoch_date + time_d).strftime('%Y%m%d')
    return date_str


def gardenlinux_epoch_from_workingtree(version_file_path: str=paths.version_path):
    '''
    determines the configured gardenlinux epoch from the current working tree.

    In particular, the contents of `VERSION` (a regular text file) are parsed, with the following
    semantics:

    - lines are stripped
    - after stripping, lines starting with `#` are ignored
    - the first non-empty line (after stripping and comment-stripping) is considered
    - from it, trailing comments are removed (with another subsequent strip)
    - the result is then expected to be one of:
      - a semver-ish version (<major>.<minor>)
        - only <major> is considered (and must be parsable to an integer
        - the parsing result is the gardenlinux epoch
      - the string literal `today`
        - in this case, the returned epoch is today's gardenlinux epoch (days since 2020-04-01)
    '''
    with open(version_file_path) as f:
        for line in f.readlines():
            if not (line := line.strip()) or line.startswith('#'): continue
            version_str = line
            if '#' in line:
                # ignore comments
                line = line.split('#', 1)[0].strip()
            break
        else:
            raise ValueError(f'did not find uncommented, non-empty line in {version_file_path}')

    # version_str may either be a semver-ish (gardenlinux only uses two components (x.y))
    try:
        epoch = int(version_str.split('.')[0])
        return epoch
    except ValueError:
        pass

    if version_str == 'today':
        return gardenlinux_epoch()

    raise ValueError(f'{version_str=} was not understood - either semver or "today" are supported')


def _enumerate_feature_files(features_dir=os.path.join(repo_root, 'features')):
    for root, _, files in os.walk(features_dir):
        for name in files:
            if not name == 'info.yaml':
                continue
            yield os.path.join(root, name)


def _deserialise_feature(feature_file):
    with open(feature_file) as f:
        parsed = yaml.safe_load(f)
    # hack: inject name from pardir
    pardir = os.path.basename(os.path.dirname(feature_file))
    parsed['name'] = pardir

    return dacite.from_dict(
        data_class=FeatureDescriptor,
        data=parsed,
        config=dacite.Config(
            cast=[
                FeatureType,
                tuple,
            ],
        ),
    )


@functools.lru_cache
def features():
    return {
        _deserialise_feature(feature_file)
        for feature_file in _enumerate_feature_files()
    }


def platforms():
    return {
        feature for feature in features() if feature.type is FeatureType.PLATFORM
    }


def modifiers():
    return {
        feature for feature in features() if feature.type is FeatureType.MODIFIER
    }


def feature_by_name(feature_name: str):
    for feature in features():
        if feature.name == feature_name:
            return feature
    raise ValueError(feature_name)
