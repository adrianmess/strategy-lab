#!/bin/bash
# Launch-template user-data for the gorig_mh12 fleet boxes (C forward /
# D reverse). Placeholders filled by build_fleet_json_orig.py:
#   REPLACE_ME_EIPALLOC  REPLACE_ME_BOOT  REPLACE_ME_NAME
exec > /var/log/gamut-userdata.log 2>&1
snap install aws-cli --classic
TOK=$(curl -sX PUT http://169.254.169.254/latest/api/token -H "X-aws-ec2-metadata-token-ttl-seconds: 300")
IID=$(curl -s -H "X-aws-ec2-metadata-token: $TOK" http://169.254.169.254/latest/meta-data/instance-id)
R=us-east-2
aws ec2 create-tags --resources $IID --tags Key=Name,Value=REPLACE_ME_NAME --region $R
aws ec2 associate-address --instance-id $IID --allocation-id REPLACE_ME_EIPALLOC --allow-reassociation --region $R
loginctl enable-linger ubuntu
timedatectl set-timezone America/Los_Angeles
sudo -u ubuntu -i bash -c "echo gamut_gorig_mh12 > ~/PLAN_NAME; aws s3 sync s3://gamut-sync-637309463295/code/ ~/ --region us-east-2; chmod +x ~/*.sh; cp ~/gamut_worker.py ~/strategy-lab/optimizer/; mkdir -p ~/strategy-lab/optimizer/campaigns; aws s3 sync s3://gamut-sync-637309463295/campaigns ~/strategy-lab/optimizer/campaigns --exclude '*' --include '*/plan.json' --include '*/config.json' --region us-east-2; aws s3 sync s3://gamut-sync-637309463295/runs ~/strategy-lab/optimizer/runs --size-only --region us-east-2; (crontab -l 2>/dev/null | grep -v -e box_s3_push -e ec2_boot -e box_autoteardown; echo \"@reboot sleep 30 && ~/REPLACE_ME_BOOT\"; echo \"*/5 * * * * ~/box_s3_push.sh\"; echo \"*/10 * * * * ~/box_autoteardown.sh\") | crontab -; ~/REPLACE_ME_BOOT"
