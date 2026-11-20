import io
import logging
import os
import re
import shutil
import subprocess
import tarfile
import time
from collections import namedtuple
from io import open
from pprint import pformat
from xml.etree import ElementTree as etree
import requests

__all__ = [
    'AddonSubmissionError',
    'create_zip',
    'get_addon_info',
    'create_addon_branch',
    'create_pull_request',
    'preflight',
    'sync_fork_branch',
]

AddonInfo = namedtuple(
    'AddonInfo', ['id', 'name', 'version', 'description', 'news', 'gh_url']
)

ADDON_REPO_URL_MASK = 'https://github.com/{}'
FORK_REPO_URL_MASK = 'https://{}:{}@github.com/{}.git'
GH_API = 'https://api.github.com'
PR_ENDPOINT_MASK = GH_API + '/repos/xbmc/{}/pulls'
FORK_ENDPOINT_MASK = GH_API + '/repos/xbmc/{}/forks'
USER_FORK_ENDPOINT_MASK = GH_API + '/repos/{}/{}'
TOKEN_URL = 'https://github.com/settings/tokens'
UPSTREAM_OWNER = 'xbmc'
HTTP_TIMEOUT = 60
OUTPUT_TAIL_LINES = 15

ADDON_VERSION_RE = re.compile(r'(<addon.+?version=")([^"]+)(")', re.I | re.DOTALL)
XBMC_PYTHON_VERSION_RE = re.compile(r'(addon="xbmc.python".+?version=")([^"]+)(")',
                                    re.I | re.DOTALL)

this_dir = os.path.dirname(os.path.abspath(__file__))

_SECRETS = set()


def register_secret(value):
    """Make sure a secret (e.g. a token) never appears in logs or error messages"""
    if value:
        _SECRETS.add(value)


def redact(text):
    """Replace all registered secrets in a string"""
    if isinstance(text, bytes):
        text = text.decode('utf-8', 'replace')
    for secret in _SECRETS:
        text = text.replace(secret, '***')
    return text


class _RedactingFilter(logging.Filter):
    def filter(self, record):
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


logging.basicConfig(level=logging.DEBUG, format='%(name)s - %(levelname)s: %(message)s')
logger = logging.getLogger('addon-submitter')
logger.addFilter(_RedactingFilter())


class AddonSubmissionError(Exception):
    pass


# (substring to look for in lowercased git output, how to fix it)
GIT_ERROR_HINTS = (
    ('workflow', 'The GitHub token is missing the "workflow" scope. It is required to push '
                 'branches that contain .github/workflows changes (e.g. when your fork is '
                 'behind upstream). Use a classic token with the "repo" and "workflow" scopes '
                 '({}), or enable --sync-fork.'.format(TOKEN_URL)),
    ('authentication failed', 'GitHub rejected the token. Check that GH_TOKEN is valid, '
                              'not expired, and belongs to GH_USERNAME.'),
    ('could not read username', 'GitHub rejected the token. Check that GH_TOKEN is valid.'),
    ('repository not found', 'The repository was not found. Check that your fork exists and '
                             'that the token has the "repo" scope.'),
    ('remote branch', 'The requested branch does not exist in xbmc/<repo>. '
                      'Check the kodi-version / --branch value.'),
    ('permission', 'GitHub denied access. Check that the token has the "repo" scope.'),
)


def hint_for(text):
    """Return a human readable fix for a known git error, or an empty string"""
    lowered = text.lower()
    for needle, hint in GIT_ERROR_HINTS:
        if needle in lowered:
            return hint
    return ''


def shell(*args, **kwargs):
    """Run a command and return its stdout. Raise AddonSubmissionError on failure.

    :param check: raise if the exit code is not 0 (default: True)
    :param cwd: working directory
    :param binary: return stdout as bytes instead of text
    """
    check = kwargs.pop('check', True)
    cwd = kwargs.pop('cwd', None)
    binary = kwargs.pop('binary', False)
    printable = redact(' '.join(args))
    logger.debug('Running: {}'.format(printable))
    try:
        proc = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd
        )
    except OSError as exc:
        raise AddonSubmissionError(
            'Unable to run "{}": {}. Is it installed and on PATH?'.format(args[0], exc)
        )
    out, err = proc.communicate()
    if check and proc.returncode != 0:
        output = redact(err) + '\n' + redact(out)
        lines = [line for line in output.splitlines() if line.strip()]
        message = 'Command failed (exit code {}): {}\n{}'.format(
            proc.returncode, printable, '\n'.join(lines[-OUTPUT_TAIL_LINES:])
        )
        hint = hint_for(output)
        if hint:
            message += '\nHint: ' + hint
        raise AddonSubmissionError(message)
    return out if binary else redact(out)


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------

def gh_request(method, url, gh_username, gh_token, **kwargs):
    """Call the GitHub API. Network failures are turned into readable errors."""
    headers = kwargs.pop('headers', {})
    headers.setdefault('Accept', 'application/vnd.github.v3+json')
    try:
        return requests.request(
            method, url, headers=headers, auth=(gh_username, gh_token),
            timeout=HTTP_TIMEOUT, **kwargs
        )
    except requests.RequestException as exc:
        raise AddonSubmissionError(
            'Unable to reach GitHub ({} {}): {}'.format(method, url, redact(str(exc)))
        )


def api_error(resp, what):
    """Build an AddonSubmissionError for an unexpected API response"""
    try:
        body = pformat(resp.json())
    except ValueError:
        body = resp.text
    message = 'GitHub API error while {}: HTTP {}\n{}'.format(what, resp.status_code, body)
    if resp.status_code == 401:
        message += '\nHint: the token is invalid or expired. Create one at {}'.format(TOKEN_URL)
    elif resp.status_code == 403:
        message += ('\nHint: the token lacks permission or you hit a rate limit. '
                    'A classic token with "repo" and "workflow" scopes is recommended ({})'
                    .format(TOKEN_URL))
    return AddonSubmissionError(message)


# ---------------------------------------------------------------------------
# Fork handling: preflight checks and sync
# ---------------------------------------------------------------------------

def get_branch_sha(owner, repo, branch, gh_username, gh_token):
    """Return the commit SHA of a branch or None if the branch does not exist"""
    resp = gh_request(
        'GET', '{}/repos/{}/{}/branches/{}'.format(GH_API, owner, repo, branch),
        gh_username, gh_token
    )
    if resp.status_code == 404:
        return None
    if not resp.ok:
        raise api_error(resp, 'reading branch {}/{}:{}'.format(owner, repo, branch))
    return resp.json()['commit']['sha']


def preflight(repo, branches, gh_username, gh_token, user_email, sync_fork=False):
    """Check everything that commonly breaks a submission before doing any work

    Raises AddonSubmissionError with an actionable message for the first
    problem found. Problems that --sync-fork fixes (a missing or outdated
    branch in your fork) are only reported as warnings.

    :param repo: addon repo name, e.g. 'repo-plugins'
    :param branches: list of target branches in the upstream repo
    :return: a list of warning strings
    """
    warnings = []

    def warn(message):
        warnings.append(message)
        logger.warning(message)

    logger.info('Running preflight checks...')
    if not user_email:
        raise AddonSubmissionError('EMAIL is not set. It is used as the git commit author email.')

    resp = gh_request('GET', GH_API + '/user', gh_username, gh_token)
    if resp.status_code == 401:
        raise AddonSubmissionError(
            'GH_TOKEN was rejected by GitHub (401). It is invalid, expired or revoked. '
            'Create a classic token with "repo" and "workflow" scopes at {}'.format(TOKEN_URL)
        )
    if not resp.ok:
        raise api_error(resp, 'checking the token')
    login = resp.json().get('login', '')
    if login.lower() != gh_username.lower():
        raise AddonSubmissionError(
            'GH_TOKEN belongs to "{}" but GH_USERNAME is "{}". The username must be the owner '
            'of the token and of the fork. In workflows, ${{{{ github.actor }}}} is the person '
            'who pushed the tag, which is not always the token owner; use your username '
            'explicitly instead.'.format(login, gh_username)
        )
    scopes_header = resp.headers.get('X-OAuth-Scopes')
    if scopes_header is None:
        warn('Unable to verify token scopes (fine-grained tokens do not report them). '
             'A classic personal access token with "repo" and "workflow" scopes is '
             'recommended: {}'.format(TOKEN_URL))
    else:
        scopes = set(s.strip() for s in scopes_header.split(',') if s.strip())
        if not scopes & {'repo', 'public_repo'}:
            raise AddonSubmissionError(
                'GH_TOKEN is missing the "repo" scope (has: {}). Create a classic token with '
                '"repo" and "workflow" scopes at {}'.format(
                    ', '.join(sorted(scopes)) or 'none', TOKEN_URL)
            )
        if 'workflow' not in scopes:
            message = (
                'GH_TOKEN is missing the "workflow" scope (has: {}). GitHub can reject pushes '
                'of branches that include .github/workflows changes without it, which '
                'happens when your fork is behind an upstream that changed a workflow. '
                'Add the scope at {}'.format(', '.join(sorted(scopes)), TOKEN_URL)
            )
            if not sync_fork:
                raise AddonSubmissionError(message + ', or enable --sync-fork.')
            warn(message + '. Continuing because --sync-fork is enabled, which keeps the '
                 'fork up to date; if syncing is rejected too, add the scope.')

    for branch in branches:
        if get_branch_sha(UPSTREAM_OWNER, repo, branch, gh_username, gh_token) is None:
            resp = gh_request(
                'GET', '{}/repos/{}/{}/branches'.format(GH_API, UPSTREAM_OWNER, repo),
                gh_username, gh_token, params={'per_page': 100}
            )
            available = ', '.join(b['name'] for b in resp.json()) if resp.ok else 'unknown'
            raise AddonSubmissionError(
                'Branch "{}" does not exist in {}/{}. Available branches: {}'.format(
                    branch, UPSTREAM_OWNER, repo, available)
            )

    resp = gh_request(
        'GET', USER_FORK_ENDPOINT_MASK.format(gh_username, repo), gh_username, gh_token
    )
    if resp.status_code == 404:
        logger.info('You have no fork of {}/{} yet, it will be created.'.format(
            UPSTREAM_OWNER, repo))
        return warnings
    if not resp.ok:
        raise api_error(resp, 'reading your fork')
    info = resp.json()
    parent = (info.get('parent') or {}).get('full_name', '')
    if not info.get('fork') or parent.lower() != '{}/{}'.format(UPSTREAM_OWNER, repo).lower():
        raise AddonSubmissionError(
            '{}/{} exists but is not a fork of {}/{}. Rename or delete it, then fork '
            '{}/{} again.'.format(gh_username, repo, UPSTREAM_OWNER, repo, UPSTREAM_OWNER, repo)
        )
    if sync_fork:
        logger.info('Skipping fork branch freshness checks, --sync-fork will handle them.')
        return warnings
    for branch in branches:
        fork_sha = get_branch_sha(gh_username, repo, branch, gh_username, gh_token)
        upstream_sha = get_branch_sha(UPSTREAM_OWNER, repo, branch, gh_username, gh_token)
        if fork_sha is None:
            warn('Your fork has no "{}" branch. This may make the push fail; '
                 'use --sync-fork to create it.'.format(branch))
        elif fork_sha != upstream_sha:
            warn('Your fork\'s "{}" branch is behind upstream. This may make the push '
                 'fail (e.g. without the "workflow" scope); use --sync-fork to update '
                 'it.'.format(branch))
    return warnings


def sync_fork_branch(repo, branch, gh_username, gh_token):
    """Make the branch of your fork identical to the upstream branch

    Tries a GitHub fast-forward merge first. If the fork branch has diverged
    (or does not exist), it is force-updated to the upstream commit. Only
    use this if your fork branch holds no work you want to keep.
    """
    logger.info('Syncing fork branch {}/{}:{} with upstream...'.format(
        gh_username, repo, branch))
    upstream_sha = get_branch_sha(UPSTREAM_OWNER, repo, branch, gh_username, gh_token)
    if upstream_sha is None:
        raise AddonSubmissionError(
            'Branch "{}" does not exist in {}/{}.'.format(branch, UPSTREAM_OWNER, repo))
    fork_sha = get_branch_sha(gh_username, repo, branch, gh_username, gh_token)
    if fork_sha == upstream_sha:
        logger.info('Fork branch "{}" is already up to date.'.format(branch))
        return 'up-to-date'
    repo_url = '{}/repos/{}/{}'.format(GH_API, gh_username, repo)
    if fork_sha is not None:
        resp = gh_request('POST', repo_url + '/merge-upstream', gh_username, gh_token,
                          json={'branch': branch})
        if resp.ok:
            logger.info('Fast-forwarded fork branch "{}" to upstream.'.format(branch))
            return 'fast-forward'
        logger.warning('Fast-forward not possible (HTTP {}), force-updating the fork '
                       'branch to match upstream.'.format(resp.status_code))
        resp = gh_request('PATCH', '{}/git/refs/heads/{}'.format(repo_url, branch),
                          gh_username, gh_token,
                          json={'sha': upstream_sha, 'force': True})
    else:
        logger.info('Fork has no "{}" branch, creating it.'.format(branch))
        resp = gh_request('POST', repo_url + '/git/refs', gh_username, gh_token,
                          json={'ref': 'refs/heads/{}'.format(branch), 'sha': upstream_sha})
    if not resp.ok:
        raise api_error(resp, 'syncing fork branch "{}"'.format(branch))
    logger.info('Fork branch "{}" now matches upstream.'.format(branch))
    return 'force-updated'


def create_personal_fork(repo, gh_username, gh_token):
    """Create a personal fork for the official repo on GitHub

    :param repo: addon repo name, e.g. 'repo-scripts' or 'repo-plugins'
    :param gh_username: GitHub username
    :param gh_token: GitHub API token
    """
    logger.info('Creating a personal fork of {}...'.format(repo))
    resp = gh_request('POST', FORK_ENDPOINT_MASK.format(repo), gh_username, gh_token)
    if not resp.ok:
        raise api_error(resp, 'forking {}/{}'.format(UPSTREAM_OWNER, repo))
    # see: https://developer.github.com/v3/repos/forks/#create-a-fork
    # this is an async operation, wait for a maximum of 5 minutes for the fork
    # to be created (with 20 seconds pause between checks)
    elapsed_time = 0
    while elapsed_time < 5 * 60:
        if user_fork_exists(repo, gh_username, gh_token):
            return
        time.sleep(20)
        elapsed_time += 20
    raise AddonSubmissionError(
        'Timed out after 5 minutes waiting for GitHub to create your fork of {}. '
        'Check github.com/{}/{} and re-run.'.format(repo, gh_username, repo)
    )


def user_fork_exists(repo, gh_username, gh_token):
    """Check if the user has a fork of the repository on Github

    :param repo: addon repo name, e.g. 'repo-scripts' or 'repo-plugins'
    :param gh_username: GitHub username
    :param gh_token: GitHub API token
    """
    resp = gh_request(
        'GET', USER_FORK_ENDPOINT_MASK.format(gh_username, repo), gh_username, gh_token,
        params={'type': 'all'}
    )
    if resp.status_code == 404:
        return False
    if not resp.ok:
        raise api_error(resp, 'looking for your fork')
    return bool(resp.json().get('fork'))


# ---------------------------------------------------------------------------
# Addon packaging and git work
# ---------------------------------------------------------------------------

def create_zip(zip_name, addon_id, subdirectory):
    """Create a .zip for an addon

    :param zip_name: .zip file name
    :type zip_name: str
    :param addon_id: addon_id ID
    :type addon_id: str
    :param subdirectory:
    :type addon_id: bool
    """
    logger.info('Creating ZIP file...')
    if subdirectory:
        shell('git', 'archive', '-o', '{}.zip'.format(zip_name), 'HEAD', '--',
              addon_id)
    else:
        shell('git', 'archive', '-o', '{}.zip'.format(zip_name),
              '--prefix', '{}/'.format(addon_id), 'HEAD')
    logger.info('ZIP created successfully.')


def get_addon_info(xml_path):
    """Extract addon info from addon.xml

    :param xml_path: path to addon.xml
    """
    if not os.path.isfile(xml_path):
        raise AddonSubmissionError(
            'addon.xml not found at {}. Check the addon id and whether '
            'the addon lives in a subdirectory (sub-directory / -s).'.format(xml_path)
        )
    tree = etree.parse(xml_path)
    addon_tag = tree.getroot()
    descr_tag = addon_tag.find('.//description[@lang="en_GB"]')
    if descr_tag is None:
        descr_tag = addon_tag.find('.//description[@lang="en"]')
        if descr_tag is None:
            raise AddonSubmissionError(
                'Unable to find an English description in addon.xml!'
            )
    news_tag = addon_tag.find('.//news')
    if news_tag is not None:
        news = news_tag.text
    else:
        news = ''
    source_tag = addon_tag.find('.//source')
    if source_tag is not None:
        gh_url = source_tag.text
    else:
        repo_slug = os.environ.get('TRAVIS_REPO_SLUG') or os.environ.get('GITHUB_REPOSITORY')
        if repo_slug:
            gh_url = ADDON_REPO_URL_MASK.format(repo_slug)
        else:
            gh_url = ''
    return AddonInfo(
        addon_tag.attrib.get('id'),
        addon_tag.attrib.get('name'),
        addon_tag.attrib.get('version'),
        descr_tag.text,
        news,
        gh_url
    )


def extract_archive(work_dir, repo_dir, addon_id, subdirectory):
    """Extract the addon from the HEAD of the git repo in work_dir into repo_dir"""
    if subdirectory:
        cmd = ['git', 'archive', '--format', 'tar', 'HEAD', '--', addon_id]
    else:
        cmd = ['git', 'archive', '--format', 'tar', 'HEAD', '--prefix', addon_id + '/']
    data = shell(*cmd, cwd=work_dir, binary=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:') as tar:
        for member in tar.getmembers():
            target = os.path.abspath(os.path.join(repo_dir, member.name))
            if not target.startswith(os.path.abspath(repo_dir) + os.sep):
                raise AddonSubmissionError(
                    'Refusing to extract unsafe path from archive: {}'.format(member.name))
        tar.extractall(repo_dir)


def create_addon_branch(work_dir, repo, branch, addon_id, version, subdirectory,
                        gh_username, gh_token, user_email, local_branch_name=None):
    """ Create and addon branch in your fork of the respective addon repo

    :param work_dir: working directory
    :param repo: addon repo name, e.g. 'repo-scripts' or 'repo-plugins'
    :param branch: repo branch that corresponds to Kodi version codename,
        e.g. 'leia'
    :param addon_id: addon ID, e.g. 'plugin.video.example'
    :param version: addon version
    :param subdirectory: addon code in a subdirectory
    :param gh_username: GitHub username
    :param gh_token: GitHub API token
    :param user_email: user's email
    :param local_branch_name: if different from addon ID
    """
    logger.info('Creating addon branch "{}"...'.format(branch))
    repo_fork = FORK_REPO_URL_MASK.format(
        gh_username, gh_token,
        '{}/{}'.format(gh_username, repo)
    )
    repo_dir = os.path.join(work_dir, repo)
    if os.path.exists(repo_dir):
        shutil.rmtree(repo_dir)
    shell('git', 'clone', '--branch', branch, '--origin', 'upstream',
          '--single-branch', 'https://github.com/{}/{}.git'.format(UPSTREAM_OWNER, repo),
          cwd=work_dir)
    git = lambda *args, **kwargs: shell('git', *args, cwd=repo_dir, **kwargs)
    git('config', 'user.name', gh_username)
    git('config', 'user.email', user_email)
    git('fetch', 'upstream')
    local_branch_name = local_branch_name or addon_id
    git('checkout', '-b', local_branch_name, 'upstream/{}'.format(branch))
    shutil.rmtree(os.path.join(repo_dir, addon_id), ignore_errors=True)
    extract_archive(work_dir, repo_dir, addon_id, subdirectory)
    git('add', '--', addon_id)
    if not git('status', '--porcelain', '--', addon_id).strip():
        raise AddonSubmissionError(
            'The addon in your repository is identical to {}/{} in upstream branch "{}", so '
            'there is nothing to submit. Did you forget to bump the version / commit your '
            'changes, or is it already merged?'.format(UPSTREAM_OWNER, repo, branch)
        )
    git('commit', '-m', '[{}] {}'.format(addon_id, version))
    git('push', '-f', '-q', repo_fork, local_branch_name)
    logger.info('Addon branch "{}" created successfully.'.format(branch))


def create_pull_request(repo, upstream_branch, local_branch, addon_info,
                        gh_username, gh_token):
    """Create a pull request in the official repo on GitHub

    :param repo: addon repo name, e.g. 'repo-scripts' or 'repo-plugins'
    :param upstream_branch: repo branch that corresponds to Kodi version codename,
        e.g. 'leia'
    :param local_branch: local branch name,
        normally it's addon ID, e.g. 'plugin.video.example'
    :param addon_info: AddonInfo object
    :param gh_username: GitHub username
    :param gh_token: GitHub API token
    """
    head = '{}:{}'.format(gh_username, local_branch)
    title = '[{}] {}'.format(local_branch, addon_info.version)
    with open(os.path.join(this_dir, 'pr-template.md'), 'r', encoding='utf-8') as fo:
        template = fo.read()
    pr_body = template.format(
        name=addon_info.name,
        id=addon_info.id,
        version=addon_info.version,
        kodi_repo_branch=upstream_branch,
        addon_gh_url=addon_info.gh_url,
        description=addon_info.description,
        news=addon_info.news
    )
    logger.info('Checking pull request...')
    resp = gh_request(
        'GET', PR_ENDPOINT_MASK.format(repo), gh_username, gh_token,
        params={'head': head, 'base': upstream_branch}
    )
    if not resp.ok:
        raise api_error(resp, 'looking for an existing pull request')
    existing = resp.json()
    if existing:
        logger.info(
            'Pull request already exists: {} (the branch was updated with the new '
            'commit).'.format(existing[0].get('html_url', head))
        )
        return
    logger.info('Submitting pull request...')
    payload = {
        'title': title,
        'head': head,
        'base': upstream_branch,
        'body': pr_body,
        'maintainer_can_modify': True,
    }
    resp = gh_request('POST', PR_ENDPOINT_MASK.format(repo), gh_username, gh_token,
                      json=payload)
    if resp.status_code != 201:
        logger.debug('Pull request payload: {}'.format(pformat(payload)))
        raise api_error(resp, 'creating the pull request')
    logger.info('Pull request submitted successfully: {}'.format(
        resp.json().get('html_url', '')))


def modify_addon_xml_for_matrix(addon_xml_path):
    logger.info('Modifying addon.xml for matrix branch')
    with open(addon_xml_path, 'r', encoding='utf-8') as fo:
        addon_xml = fo.read()
    addon_version_match = ADDON_VERSION_RE.search(addon_xml)
    if addon_version_match is None:
        raise AddonSubmissionError('Unable to parse addon version in addon.xml')
    xbmc_python_version_match = XBMC_PYTHON_VERSION_RE.search(addon_xml)
    if xbmc_python_version_match is None:
        raise AddonSubmissionError('Unable to parse xbmc.python version in addon.xml')
    addon_version = addon_version_match.group(2)
    matrix_addon_version = addon_version + '+matrix.1'
    matrix_addon_version_mask = r'\g<1>{}\g<3>'.format(matrix_addon_version)
    addon_xml = ADDON_VERSION_RE.sub(matrix_addon_version_mask, addon_xml)
    addon_xml = XBMC_PYTHON_VERSION_RE.sub(r'\g<1>3.0.0\g<3>', addon_xml)
    write_addonxml(addon_xml_path, addon_xml)


def get_addonxml_content(addon_xml_path):
    logger.info('Getting addon.xml file content')
    if not os.path.isfile(addon_xml_path):
        raise AddonSubmissionError(
            'addon.xml not found at {}. Check the addon id and whether '
            'the addon lives in a subdirectory (sub-directory / -s).'.format(addon_xml_path)
        )
    with open(addon_xml_path, 'r', encoding='utf-8') as addonxml:
        return addonxml.read()


def write_addonxml(addon_xml_path, addon_xml):
    with open(addon_xml_path, 'w', encoding='utf-8') as fo:
        fo.write(addon_xml)


def create_git_commit(message, cwd=None):
    shell('git', 'commit', '-a', '-m', message, cwd=cwd)
