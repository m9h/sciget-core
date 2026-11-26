%global pypi_name sciget-core

Name:           python-%{pypi_name}
Version:        0.1.0
Release:        1%{?dist}
Summary:        Start and manage Sciget/Neurodesk container sessions

License:        BSD-3-Clause
URL:            https://github.com/scigetorg/neurodesk-app
# No public repository yet: the tarball is `git archive` of the local repo,
#   git archive --prefix=sciget-core-%%{version}/ -o sciget-core-%%{version}.tar.gz HEAD
Source0:        sciget-core-%{version}.tar.gz

BuildArch:      noarch

BuildRequires:  python3-devel
BuildRequires:  python3-pytest
BuildRequires:  desktop-file-utils

%global _description %{expand:
sciget-core starts Sciget and Neurodesk desktop sessions (JupyterLab with
hundreds of scientific tools) in containers, from the command line or a local
web UI, without Electron. It drives rootless Podman, Docker or Apptainer,
does not run containers privileged, and can use a CernVM-FS repository
mounted on the host (the cvmfs and cvmfs-config-neurodesk packages).}

%description %_description

%package -n     %{pypi_name}
Summary:        %{summary}
Provides:       python3-%{pypi_name} = %{version}-%{release}
# A container engine is needed to start sessions; Podman is the default.
Recommends:     podman
Suggests:       apptainer
# Lets sessions use CVMFS from the host instead of mounting it in the container.
Suggests:       cvmfs-config-neurodesk

%description -n %{pypi_name} %_description

%prep
%autosetup -n sciget-core-%{version}

%generate_buildrequires
%pyproject_buildrequires

%build
%pyproject_wheel

%install
%pyproject_install
%pyproject_save_files sciget_core
install -Dpm 0644 packaging/sciget.desktop \
    %{buildroot}%{_datadir}/applications/sciget.desktop

%check
desktop-file-validate %{buildroot}%{_datadir}/applications/sciget.desktop
%pyproject_check_import
# The web UI must be in the wheel, or `sciget serve` has nothing to serve.
test -f %{buildroot}%{python3_sitelib}/sciget_core/web/index.html
test -f %{buildroot}%{python3_sitelib}/sciget_core/domains/neurodesk.yml
# Engines are faked in the tests; nothing here starts a container.
# -o pythonpath= drops pyproject's src/ entry so the INSTALLED package is tested.
%pytest -p no:cacheprovider -o pythonpath= tests
%{buildroot}%{_bindir}/sciget --version

%files -n %{pypi_name} -f %{pyproject_files}
%license LICENSE
%doc README.md docs/api.md
%{_bindir}/sciget
%{_datadir}/applications/sciget.desktop

%changelog
* Thu Nov 26 2026 Morgan Hough <morgan.hough@gmail.com> - 0.1.0-1
- Initial package
