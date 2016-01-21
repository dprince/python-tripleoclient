python setup.py install

TEMPLATES_BASE="/usr/share/openstack-tripleo-heat-templates"
openstack overcloud deploy --templates=$TEMPLATES_BASE \
-e $TEMPLATES_BASE/environments/network-isolation.yaml \
-e $TEMPLATES_BASE/environments/net-single-nic-with-vlans.yaml
