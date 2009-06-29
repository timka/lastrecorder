#!/bin/sh
set -e
NAME=`dpkg-parsechangelog | awk '/^Source: / {print $2}'`
PACKAGE_VERSION=`dpkg-parsechangelog | awk '/^Version: / {print $2}'`
CODE_VERSION=`python -c "execfile('$NAME/release.py'); print version,"`
echo "$NAME: PACKAGE_VERSION=$PACKAGE_VERSION CODE_VERSION=$CODE_VERSION"

# Newer version in the source code
python -c "import sys; sys.exit(not '$CODE_VERSION' > '$PACKAGE_VERSION')" && {
    dch -v $CODE_VERSION
    hg ci
    PACKAGE_VERSION=$CODE_VERSION
    hg tag $PACKAGE_VERSION
}
VERSION=$PACKAGE_VERSION
PREFIX=${NAME}_${VERSION}
DIST=${NAME}-${VERSION}

rm -f ${PREFIX}*.{deb,dsc,changes,tar.gz,upload}
hg archive $DIST
cd $DIST
dpkg-buildpackage -uc -us
cd ..
rm -rf $DIST
