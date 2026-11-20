import argparse
import os
import sys
from . import utils

work_dir = os.getcwd()


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(
        description='CLI utility for submitting Kodi addons '
        'to the official addon repository'
    )
    parser.add_argument('addon_id', nargs='?', help='Addon ID')
    parser.add_argument('-z', '--zip', action='store_true',
                        help='Create a .zip file')
    parser.add_argument('-r', '--repo', nargs='?', default='',
                        help='GitHub repo for this addon type')
    parser.add_argument('-b', '--branch', nargs='?', default='',
                        help='Addon repo branch (Kodi version codename)')
    parser.add_argument('--push-branch', action='store_true',
                        help='Push addon branch to addon repo fork')
    parser.add_argument('--pull-request', action='store_true',
                        help='Create a pull request')
    parser.add_argument('-s', '--subdirectory', action='store_true',
                        help='Addon is stored in its own directory within the git repo')
    parser.add_argument('-m', '--matrix', action='store_true',
                        help='Submit to the matrix branch as well if --pull-request or '
                             '--push-branch or create an addition zip file for matrix if --matrix')
    parser.add_argument('--sync-fork', action='store_true',
                        help='Before submitting, make the target branch(es) of your fork '
                             'identical to upstream (fast-forward, or force-update if the '
                             'fork branch diverged). Do not use if your fork branch has '
                             'work you want to keep.')
    parser.add_argument('--check', action='store_true',
                        help='Only run the preflight checks (token, fork, branches) and exit')
    parser.add_argument('--gh-username', nargs='?', default='',
                        help='GitHub username')
    parser.add_argument('--gh-token', nargs='?', default='',
                        help='GitHub API token')
    parser.add_argument('--user-email', nargs='?', default='',
                        help='User email')
    return parser.parse_args(argv)


def require_submission_settings(args, gh_username, gh_token, user_email):
    if not (args.repo and args.branch):
        raise utils.AddonSubmissionError(
            'Both --repo and --branch arguments must be defined '
            '(e.g. -r repo-plugins -b nexus).'
        )
    if not (gh_username and gh_token and user_email):
        missing = [name for name, value in (
            ('GH_USERNAME', gh_username), ('GH_TOKEN', gh_token), ('EMAIL', user_email)
        ) if not value]
        raise utils.AddonSubmissionError(
            'Missing required settings: {}. Provide them as environment variables '
            '(in GitHub Actions: as secrets passed via "env:") or as command line '
            'arguments.'.format(', '.join(missing))
        )


def target_branches(args):
    branches = [args.branch]
    if args.matrix and args.branch != 'matrix':
        branches.append('matrix')
    return branches


def run_preflight(args, gh_username, gh_token, user_email):
    require_submission_settings(args, gh_username, gh_token, user_email)
    utils.preflight(
        args.repo, target_branches(args), gh_username, gh_token, user_email,
        sync_fork=args.sync_fork
    )


def submit_matrix(args, addon_xml_path, gh_username, gh_token, user_email):
    """Submit to the matrix branch"""
    os.chdir(work_dir)
    original_xml = utils.get_addonxml_content(addon_xml_path)
    utils.modify_addon_xml_for_matrix(addon_xml_path)
    utils.create_git_commit('Modify versions for matrix branch', cwd=work_dir)
    try:
        addon_info = utils.get_addon_info(addon_xml_path)
        local_branch_name = '{}@{}'.format(args.addon_id, 'matrix')
        utils.create_addon_branch(
            work_dir, args.repo, 'matrix', args.addon_id, addon_info.version,
            args.subdirectory, gh_username, gh_token, user_email,
            local_branch_name=local_branch_name
        )
        if args.pull_request:
            utils.create_pull_request(
                args.repo, 'matrix', local_branch_name, addon_info,
                gh_username, gh_token
            )
    finally:
        utils.write_addonxml(addon_xml_path, original_xml)


def run(args):
    gh_username = os.getenv('GH_USERNAME') or args.gh_username
    gh_token = os.getenv('GH_TOKEN') or args.gh_token
    user_email = os.getenv('EMAIL') or args.user_email
    utils.register_secret(gh_token)

    if args.check:
        run_preflight(args, gh_username, gh_token, user_email)
        utils.logger.info('Preflight checks passed.')
        return
    if not args.addon_id:
        raise utils.AddonSubmissionError('The addon_id argument is required.')

    submitting = args.push_branch or args.pull_request
    if submitting:
        # fail before doing any work if the setup is broken
        run_preflight(args, gh_username, gh_token, user_email)

    addon_xml_path = os.path.join(work_dir, args.addon_id if args.subdirectory else '', 'addon.xml')
    addon_xml_originalcontent = utils.get_addonxml_content(addon_xml_path)
    addon_info = utils.get_addon_info(addon_xml_path)
    if args.zip:
        utils.create_zip(
            args.addon_id + '-' + addon_info.version, args.addon_id, args.subdirectory
        )
        if args.matrix:
            utils.modify_addon_xml_for_matrix(addon_xml_path)
            addon_info = utils.get_addon_info(addon_xml_path)
            utils.create_zip(
                args.addon_id + '-' + addon_info.version, args.addon_id, args.subdirectory
            )
            # restore addon.xml original content
            utils.write_addonxml(addon_xml_path, addon_xml_originalcontent)
    if not submitting:
        return

    # restore addon info (a zip modifying addon.xml might have been created before)
    addon_info = utils.get_addon_info(addon_xml_path)
    # fork the repo if the user does not have a personal repo fork
    if not utils.user_fork_exists(args.repo, gh_username, gh_token):
        utils.create_personal_fork(args.repo, gh_username, gh_token)
    if args.sync_fork and utils.user_fork_exists(args.repo, gh_username, gh_token):
        for branch in target_branches(args):
            utils.sync_fork_branch(args.repo, branch, gh_username, gh_token)
    local_branch_name = '{}@{}'.format(args.addon_id, args.branch)
    utils.create_addon_branch(
        work_dir, args.repo, args.branch, args.addon_id, addon_info.version,
        args.subdirectory, gh_username, gh_token, user_email,
        local_branch_name=local_branch_name
    )
    if args.pull_request:
        utils.create_pull_request(
            args.repo, args.branch, local_branch_name, addon_info, gh_username, gh_token
        )
    if args.matrix:
        submit_matrix(args, addon_xml_path, gh_username, gh_token, user_email)


def main():
    args = parse_arguments()
    try:
        run(args)
    except utils.AddonSubmissionError as exc:
        utils.logger.error('{}'.format(exc))
        sys.exit(1)


if __name__ == '__main__':
    main()
