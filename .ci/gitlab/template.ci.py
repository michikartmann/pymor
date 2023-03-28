#!/usr/bin/env python3

import os
import jinja2
from pathlib import Path  # python3 only
from dotenv import dotenv_values
import sys
import gitlab
from itertools import product

tpl = r'''# THIS FILE IS AUTOGENERATED -- DO NOT EDIT #
#   Edit and Re-run .ci/gitlab/template.ci.py instead       #

stages:
  - sanity
  - test
  - build
  - install_checks
  - deploy

{% macro never_on_schedule_rule(exclude_github=False) -%}
rules:
    - if: $CI_PIPELINE_SOURCE == "schedule"
      when: never
{%- if exclude_github %}
    - if: $CI_COMMIT_REF_NAME =~ /^github.*/
      when: never
{%- endif %}
    - when: on_success
{%- endmacro -%}

#************ definition of base jobs *********************************************************************************#

.test_base:
    retry:
        max: 2
        when:
            - runner_system_failure
            - api_failure
    tags:
      - autoscaling
    rules:
        - if: $CI_COMMIT_REF_NAME =~ /^staging.*/
          when: never
        - when: on_success
    variables:
        PYPI_MIRROR_TAG: {{pypi_mirror_tag}}
        CI_IMAGE_TAG: {{ci_image_tag}}
        PYMOR_HYPOTHESIS_PROFILE: ci
        PYMOR_PYTEST_EXTRA: ""
        BINDERIMAGE: ${CI_REGISTRY_IMAGE}/binder:${CI_COMMIT_REF_SLUG}

.pytest:
    extends: .test_base
    tags:
      - long execution time
      - autoscaling
    environment:
        name: unsafe
    stage: test
    after_script:
      - .ci/gitlab/after_script.bash
    cache:
        key: same_db_on_all_runners
        paths:
          - .hypothesis
    artifacts:
        when: always
        name: "$CI_JOB_STAGE-$CI_COMMIT_REF_SLUG"
        expire_in: 3 months
        paths:
            - src/pymortests/testdata/check_results/*/*_changed
            - docs/source/*_extracted.py
            - coverage*
            - memory_usage.txt
            - .hypothesis
            - test_results*.xml

{# note: only Vanilla and numpy runs generate coverage or test_results so we can skip others entirely here #}
.submit:
    extends: .test_base
    image: {{registry}}/pymor/ci_sanity:${CI_IMAGE_TAG}
    variables:
        XDG_CACHE_DIR: /tmp
    retry:
        max: 2
        when:
            - always
    environment:
        name: safe
    {{ never_on_schedule_rule(exclude_github=True) }}
    stage: deploy
    script: .ci/gitlab/submit.bash

.docker-in-docker:
    tags:
      - docker-in-docker
      - autoscaling
    extends: .test_base
    timeout: 45 minutes
    retry:
        max: 2
        when:
            - runner_system_failure
            - stuck_or_timeout_failure
            - api_failure
            - unknown_failure
            - job_execution_timeout
    {# this is intentionally NOT moving with CI_IMAGE_TAG #}
    image: {{registry}}/pymor/docker-in-docker:2022.1.0@sha256:c912491b287e5e539efc7e160a4196bfef4e3c71934ff70e66afc4da88470254

    variables:
        DOCKER_DRIVER: overlay2
    before_script:
        - 'export SHARED_PATH="${CI_PROJECT_DIR}/shared"'
        - mkdir -p ${SHARED_PATH}
        - docker login -u $CI_REGISTRY_USER -p $CI_REGISTRY_PASSWORD $CI_REGISTRY
    services:
        - name: {{registry}}/pymor/docker-in-docker:2022.1.0@sha256:c912491b287e5e539efc7e160a4196bfef4e3c71934ff70e66afc4da88470254
          alias: docker
    environment:
        name: unsafe


# this should ensure binderhubs can still build a runnable image from our repo
.binder:
    extends: .docker-in-docker
    stage: install_checks
    needs: ["ci setup"]
    {{ never_on_schedule_rule() }}
    variables:
        USER: juno

.check_wheel:
    extends: .test_base
    stage: install_checks
    timeout: 10 minutes
    dependencies: ["sdist_and_wheel"]
    needs: ["sdist_and_wheel"]
    {{ never_on_schedule_rule() }}
    services:
      - name: {{registry}}/pymor/devpi:${PYPI_MIRROR_TAG}
        alias: pymor__devpi
    before_script:
      # bump to our minimal version
      - python3 -m pip install "devpi-client"
      - python3 -m pip install "https://m.devpi.net/fschulze/dev/+f/6ac/e7aaa2d1196f1/devpi_common-3.7.1.dev0-py2.py3-none-any.whl"
      - devpi use http://pymor__devpi:3141/root/public --set-cfg
      - devpi login root --password ''
      - devpi upload --from-dir --formats=* ./dist/*.whl
      - python3 -m pip install pip~=22.0
      - python3 -m pip remove -y pymor || true
    # the docker service adressing fails on other runners
    tags: [mike]

.sanity_checks:
    extends: .test_base
    image: {{registry}}/pymor/ci_sanity:${CI_IMAGE_TAG}
    stage: sanity
#******** end definition of base jobs *********************************************************************************#

# https://docs.gitlab.com/ee/ci/yaml/README.html#workflowrules-templates
include:
  - template: 'Workflows/Branch-Pipelines.gitlab-ci.yml'

#******* sanity stage

# this step makes sure that on older python our install fails with
# a nice message ala "python too old" instead of "SyntaxError"
verify setup.py:
    extends: .sanity_checks
    script:
        - python3 setup.py egg_info

ci setup:
    extends: .sanity_checks
    script:
        - ${CI_PROJECT_DIR}/.ci/gitlab/ci_sanity_check.bash "{{ ' '.join(pythons) }}"

#****** test stage

{%- for script, py, para in matrix %}
{{script}} {{py[0]}} {{py[2]}}:
    extends: .pytest
    {{ never_on_schedule_rule() }}
    variables:
        COVERAGE_FILE: coverage_{{script}}__{{py}}
    {%- if script == "mpi" %}
    retry:
        max: 2
        when: always
    {%- endif %}
    services:
    {%- if script == "oldest" %}
        - name: {{registry}}/pymor/pypi-mirror_oldest_py{{py}}:${PYPI_MIRROR_TAG}
          alias: pypi_mirror
    {%- elif script in ["pip_installed", "numpy_git"] %}
        - name: {{registry}}/pymor/pypi-mirror_stable_py{{py}}:${PYPI_MIRROR_TAG}
          alias: pypi_mirror
    {%- endif %}
    image: {{registry}}/pymor/testing_py{{py}}:${CI_IMAGE_TAG}
    script:
        - |
          if [[ "$CI_COMMIT_REF_NAME" == *"github/PR_"* ]]; then
            echo selecting hypothesis profile "ci_pr" for branch $CI_COMMIT_REF_NAME
            export PYMOR_HYPOTHESIS_PROFILE="ci_pr"
          else
            echo selecting hypothesis profile "ci" for branch $CI_COMMIT_REF_NAME
            export PYMOR_HYPOTHESIS_PROFILE="ci"
          fi
        - ./.ci/gitlab/test_{{script}}.bash
{%- endfor %}

{%- for py in pythons %}
ci_weekly {{py[0]}} {{py[2]}}:
    extends: .pytest
    timeout: 5h
    variables:
        COVERAGE_FILE: coverage_ci_weekly
    rules:
        - if: $CI_PIPELINE_SOURCE == "schedule"
          when: always
    services:
        - name: {{registry}}/pymor/pypi-mirror_stable_py{{py}}:${PYPI_MIRROR_TAG}
          alias: pypi_mirror
    image: {{registry}}/pymor/testing_py{{py}}:${CI_IMAGE_TAG}
    {# PYMOR_HYPOTHESIS_PROFILE is overwritten from web schedule settings #}
    script: ./.ci/gitlab/test_vanilla.bash
{%- endfor %}

submit coverage:
    extends: .submit
    artifacts:
        when: always
        name: "coverage_reports"
        paths:
            - reports/
    dependencies:
    {%- for script, py, para in matrix if script in ['tutorials', 'vanilla', 'oldest', 'numpy_git', 'mpi'] %}
        - {{script}} {{py[0]}} {{py[2]}}
    {%- endfor %}

coverage html:
    extends: .submit
    needs: ["submit coverage"]
    dependencies: ["submit coverage"]
    artifacts:
        name: "coverage_html"
        paths:
            - coverage_html
    before_script:
        - apk add py3-coverage
    script:
        - coverage combine reports/coverage*
        - coverage html --directory coverage_html

{%- for py in pythons %}
submit ci_weekly {{py[0]}} {{py[2]}}:
    extends: .submit
    rules:
        - if: $CI_PIPELINE_SOURCE == "schedule"
          when: always
    dependencies:
        - ci_weekly {{py[0]}} {{py[2]}}
    needs: ["ci_weekly {{py[0]}} {{py[2]}}"]
{%- endfor %}


binder base image:
    extends: .binder
    stage: build
    script:
        - docker build --build-arg CI_IMAGE_TAG=${CI_IMAGE_TAG} -t ${BINDERIMAGE} -f .ci/gitlab/Dockerfile.binder.base .
        - docker login -u $CI_REGISTRY_USER -p $CI_REGISTRY_PASSWORD $CI_REGISTRY
        - docker run ${BINDERIMAGE} ipython -c "from pymor.basic import *"
        - docker push ${BINDERIMAGE}

local docker:
    extends: .binder
    script:
        - make docker_image
        - make DOCKER_CMD="ipython -c 'from pymor.basic import *'" docker_exec

{% for url in binder_urls %}
trigger_binder {{loop.index}}/{{loop.length}}:
    extends: .test_base
    stage: deploy
    image: harbor.uni-muenster.de/proxy-docker/library/alpine:3.16
    rules:
        - if: $CI_COMMIT_REF_NAME == "main"
          when: on_success
        - if: $CI_COMMIT_TAG != null
          when: on_success
    before_script:
        - apk --update add bash py3-pip
        - pip3 install requests
    script:
        - python3 .ci/gitlab/trigger_binder.py "{{url}}/${CI_COMMIT_REF}"
{% endfor %}

sdist_and_wheel:
    extends: .sanity_checks
    stage: build
    needs: ["ci setup"]
    {{ never_on_schedule_rule() }}
    artifacts:
        paths:
        - dist/pymor*.whl
        - dist/pymor*.tar.gz
        expire_in: 1 week
    script: python3 -m build

pypi:
    extends: .test_base
    image: harbor.uni-muenster.de/proxy-docker/library/alpine:3.16
    stage: deploy
    needs:
    dependencies:
      - sdist_and_wheel
    {{ never_on_schedule_rule(exclude_github=True) }}
    variables:
        ARCHIVE_DIR: pyMOR_wheels-${CI_COMMIT_REF_NAME}
    artifacts:
        paths:
         - ${CI_PROJECT_DIR}/${ARCHIVE_DIR}/pymor*whl
         - ${CI_PROJECT_DIR}/${ARCHIVE_DIR}/pymor*tar.gz
        expire_in: 6 months
        name: pymor-wheels
    before_script:
        - apk add py3-pip twine bash
    script:
        - ${CI_PROJECT_DIR}/.ci/gitlab/pypi_deploy.bash
    environment:
        name: safe

{% for OS, PY in testos %}
from wheel {{loop.index}}/{{loop.length}}:
    extends: .check_wheel
    image: {{registry}}/pymor/deploy_checks_{{OS}}:${CI_IMAGE_TAG}
    script:
      - echo "Testing wheel install on {{OS}} with Python {{PY}}"
      - python3 -m pip freeze --all
      - devpi install pymor[full]
{% endfor %}

{%- for py in pythons %}
docs build {{py[0]}} {{py[2]}}:
    extends: .test_base
    tags: [mike]
    rules:
        - if: $CI_PIPELINE_SOURCE == "schedule"
          when: never
        - when: on_success
    services:
        - name: {{registry}}/pymor/pypi-mirror_stable_py{{py}}:${PYPI_MIRROR_TAG}
          alias: pypi_mirror
    image: {{registry}}/pymor/jupyter_py{{py}}:${CI_IMAGE_TAG}
    script:
        - ${CI_PROJECT_DIR}/.ci/gitlab/test_docs.bash
    stage: build
    needs: ["ci setup"]
    artifacts:
        paths:
            - docs/_build/html
            - docs/error.log
{% endfor %}

docs:
    extends: .docker-in-docker
    # makes sure this doesn't land on the test runner
    tags: [mike]
    image: harbor.uni-muenster.de/proxy-docker/library/alpine:3.16
    stage: deploy
    resource_group: docs_deploy
    needs: ["docs build 3 9", "binder base image"]
    dependencies: ["docs build 3 9", "binder base image"]
    before_script:
        - apk --update add make py3-pip bash py3-ruamel.yaml.clib
        - pip3 install jinja2 jupyter-repo2docker
    script:
        - ${CI_PROJECT_DIR}/.ci/gitlab/deploy_docs.bash
    rules:
        - if: $CI_PIPELINE_SOURCE == "schedule"
          when: never
        - if: $CI_COMMIT_REF_NAME =~ /^github\/PR_.*/
          when: never
        - when: on_success
    environment:
        name: safe

# THIS FILE IS AUTOGENERATED -- DO NOT EDIT #
#   Edit and Re-run .ci/gitlab/template.ci.py instead       #

'''  # noqa


tpl = jinja2.Template(tpl)
pythons = ['3.8', '3.9']
oldest = [pythons[0]]
newest = [pythons[-1]]
test_scripts = [
    ("mpi", pythons, 1),
    ("pip_installed", pythons, 1),
    ("tutorials", pythons, 1),
    ("vanilla", pythons, 1),
    ("oldest", oldest, 1),
    ("cpp_demo", pythons, 1),
]
# these should be all instances in the federation
binder_urls = [f'https://{sub}.mybinder.org/build/gh/pymor/pymor' for sub in ('gke', 'ovh', 'gesis')]
testos = [('fedora', '3.9'), ('debian-bullseye', '3.9')]

env_path = Path(os.path.dirname(__file__)) / '..' / '..' / '.env'
env = dotenv_values(env_path)
ci_image_tag = env['CI_IMAGE_TAG']
pypi_mirror_tag = env['PYPI_MIRROR_TAG']
registry = "zivgitlab.wwu.io/pymor/docker"
with open(os.path.join(os.path.dirname(__file__), 'ci.yml'), 'wt') as yml:
    matrix = [(sc, py, pa) for sc, pythons, pa in test_scripts for py in pythons]
    yml.write(tpl.render(**locals()))

try:
    token = sys.argv[1]
except IndexError:
    print("not checking image availability, no token given")
    sys.exit(0)

print("Checking image availability\n")
gl = gitlab.Gitlab("https://zivgitlab.uni-muenster.de", private_token=token)
gl.auth()

pymor_id = 2758
pymor = gl.projects.get(pymor_id)

image_tag = ci_image_tag
mirror_tag = pypi_mirror_tag
images = ["testing", "jupyter"]
mirrors = [f"{r}_py{py}"
           for r, py in product(["pypi-mirror_stable", "pypi-mirror_oldest"], pythons)]
images = [f"{r}_py{py}" for r, py in product(images, pythons)]
images += [f"deploy_checks_{os}" for os, _ in testos] + ["python_3.9"]

missing = set((r, mirror_tag) for r in mirrors) | set((r, image_tag) for r in images)
img_count = len(missing)
for repo in pymor.repositories.list(all=True):
    wanted = None
    match_name = repo.name.replace("pymor/", "")

    if match_name in mirrors:
        wanted = mirror_tag
    elif match_name in images:
        wanted = image_tag
    if wanted:
        try:
            tag = repo.tags.get(id=wanted)
            missing.remove((match_name, wanted))
        except gitlab.exceptions.GitlabGetError:
            continue

if len(missing):
    try:
        from rich.console import Console
        from rich.table import Table
        table = Table("image", "tag", title="Not found in Container Registry")
        for el in sorted(missing):
            table.add_row(*el)
        console = Console()
        console.print(table)
        console.print(f"Missing {len(missing)} of {img_count} image:tag pairs")
    except (ImportError, ModuleNotFoundError):
        print(f"Missing {len(missing)} of {img_count} image:tag pairs")
        print(missing)
    sys.exit(1)
