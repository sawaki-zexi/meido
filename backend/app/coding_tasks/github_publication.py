"""Fixed GitHub HTTPS protocol and an isolated, host-owned Git transport."""
from __future__ import annotations

import json
from pathlib import Path
import re
from urllib.parse import quote

import httpx

from .git_repository import git_environment


class GithubPlatform:
    def __init__(self, token, remote, *, base_url='https://api.github.com'):
        self.remote = remote
        self.check = lambda: 30
        self.client = httpx.Client(base_url=base_url, trust_env=False, follow_redirects=False,
            timeout=httpx.Timeout(20, connect=5), headers={'Authorization': 'Bearer ' + token,
                'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2026-03-10'})
        self.path = '/repos/' + quote(remote['owner'], safe='') + '/' + quote(remote['repository'], safe='')

    def _request(self, method, path, *, data=None, absent=False):
        remaining = self.check()
        with self.client.stream(method, path, json=data, timeout=min(20, remaining)) as response:
            if absent and response.status_code == 404:
                return None
            if response.status_code != (201 if method == 'POST' else 200):
                raise ValueError('GitHub operation was not confirmed')
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > 256 * 1024:
                    raise ValueError('GitHub response exceeds the operation budget')
            value = json.loads(body)
        return value

    def repository(self):
        value = self._request('GET', self.path)
        full_name = self.remote['owner'] + '/' + self.remote['repository']
        if (not isinstance(value, dict) or type(value.get('id')) is not int
                or value.get('full_name', '').lower() != full_name.lower()
                or value.get('owner', {}).get('login', '').lower() != self.remote['owner'].lower()
                or value.get('clone_url', '').lower() != self.remote['url'].lower()):
            raise ValueError('GitHub repository differs from approved remote')
        return value

    def reference(self, branch):
        value = self._request('GET', self.path + '/git/ref/heads/' + quote(branch, safe=''), absent=True)
        if value is None:
            return None
        if (not isinstance(value, dict) or value.get('ref') != 'refs/heads/' + branch
                or value.get('object', {}).get('type') != 'commit'
                or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', value['object'].get('sha', ''))):
            raise ValueError('GitHub ref identity is invalid')
        return value['object']['sha']

    def pulls(self, branch, base):
        values = []
        for page in (1, 2):
            path = (self.path + '/pulls?state=all&head=' + quote(self.remote['owner'] + ':' + branch, safe='')
                + '&base=' + quote(base, safe='') + '&per_page=100&page=' + str(page))
            result = self._request('GET', path)
            if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
                raise ValueError('GitHub pull query is invalid')
            values.extend(result)
            if len(result) < 100:
                return values
        raise ValueError('GitHub pull query exceeds its observation budget')

    def create(self, candidate):
        value = self._request('POST', self.path + '/pulls', data={'title': candidate['title'],
            'body': candidate['body'], 'head': candidate['publishBranch'], 'base': candidate['baseRef'],
            'draft': True, 'maintainer_can_modify': False})
        if not isinstance(value, dict) or type(value.get('number')) is not int or value['number'] <= 0:
            raise ValueError('GitHub pull creation identity is unavailable')
        return self.pull(value['number'])

    def pull(self, number):
        if type(number) is not int or number <= 0:
            raise ValueError('GitHub pull number is invalid')
        value = self._request('GET', self.path + '/pulls/' + str(number))
        if not isinstance(value, dict) or value.get('number') != number:
            raise ValueError('GitHub pull identity changed')
        return value

    def verify(self, pull, candidate, repository):
        head, base = pull.get('head', {}), pull.get('base', {})
        number = pull.get('number')
        url = 'https://github.com/' + repository['full_name'] + '/pull/' + str(number)
        if (type(number) is not int or number <= 0 or pull.get('html_url') != url
                or pull.get('state') != 'open' or pull.get('draft') is not True
                or head.get('ref') != candidate['publishBranch'] or head.get('sha') != candidate['commitSha']
                or base.get('ref') != candidate['baseRef']
                or base.get('sha') != candidate['baseHead']
                or any(side.get('repo', {}).get('id') != repository['id']
                    or side.get('repo', {}).get('full_name', '').lower() != repository['full_name'].lower()
                    for side in (head, base))
                or pull.get('title') != candidate['title'] or (pull.get('body') or '') != candidate['body']):
            raise ValueError('GitHub Draft PR differs from exact owner approval')
        return {'number': number, 'url': url, 'headSha': head['sha'], 'headRef': head['ref'],
                'baseRef': base['ref'], 'repositoryId': repository['id'], 'draft': True, 'state': 'open'}

    def close(self):
        self.client.close()


class GitTransport:
    """Copy approved objects without credentials, then push from private config."""
    def __init__(self, directory, owner, candidate, token, *, destination=None):
        self.directory, self.owner, self.candidate = Path(directory), owner, candidate
        self.token, self.destination = token, destination

    def push(self, worktree):
        candidate, owner = self.candidate, self.owner
        directory = self.directory
        directory.mkdir(exist_ok=False)
        bundle, bare = directory / 'objects.bundle', directory / 'objects.git'
        owner.run(worktree, 'bundle', 'create', str(bundle), 'refs/heads/' + candidate['branch'])
        if bundle.is_symlink() or bundle.stat().st_size > 2 * 1024 * 1024 * 1024:
            raise ValueError('approved object bundle exceeds its storage budget')
        heads = owner.run(worktree, 'bundle', 'list-heads', str(bundle)).splitlines()
        if heads != [candidate['commitSha'] + ' refs/heads/' + candidate['branch']]:
            raise ValueError('approved source branch changed during object capture')
        bare.mkdir()
        owner.run(bare, 'init', '--bare')
        owner.run(bare, 'bundle', 'unbundle', str(bundle))
        if owner.run(bare, 'rev-parse', candidate['commitSha'] + '^{tree}') != candidate['tree']:
            raise ValueError('private object stage differs from approved tree')
        env = git_environment()
        settings = {'http.https://github.com/.extraheader': 'Authorization: Bearer ' + self.token,
            'http.followRedirects': 'false', 'protocol.allow': 'never',
            'protocol.https.allow': 'always', 'push.followTags': 'false',
            'push.recurseSubmodules': 'no', 'transfer.fsckObjects': 'true'}
        if self.destination is not None:
            settings['protocol.file.allow'] = 'always'
        start = int(env['GIT_CONFIG_COUNT'])
        for offset, (key, value) in enumerate(settings.items(), start):
            env['GIT_CONFIG_KEY_' + str(offset)] = key
            env['GIT_CONFIG_VALUE_' + str(offset)] = value
        env['GIT_CONFIG_COUNT'] = str(start + len(settings))
        for key in ('HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'XDG_CONFIG_HOME'):
            env[key] = str(directory)
        result = owner.run(bare, 'push', '--porcelain', '--no-verify', '--no-follow-tags',
            self.destination or candidate['remote']['url'],
            candidate['commitSha'] + ':refs/heads/' + candidate['publishBranch'], env=env, limit=65536)
        if '[new branch]' not in result:
            raise ValueError('push did not confirm creation of the approved owned branch')
