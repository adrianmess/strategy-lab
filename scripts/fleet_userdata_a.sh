#!/bin/bash
exec > /var/log/gamut-userdata.log 2>&1
snap install aws-cli --classic
TOK=$(curl -sX PUT http://169.254.169.254/latest/api/token -H "X-aws-ec2-metadata-token-ttl-seconds: 300")
IID=$(curl -s -H "X-aws-ec2-metadata-token: $TOK" http://169.254.169.254/latest/meta-data/instance-id)
R=us-east-2
aws ec2 create-tags --resources $IID --tags Key=Name,Value=gamut-worker-2c --region $R
aws ec2 associate-address --instance-id $IID --allocation-id eipalloc-0d7a1245b58d910ba --allow-reassociation --region $R
loginctl enable-linger ubuntu
sudo -u ubuntu -i bash -c "aws s3 sync s3://gamut-sync-637309463295/code/ ~/ --region us-east-2; chmod +x ~/*.sh; cp ~/gamut_worker.py ~/strategy-lab/optimizer/; aws s3 sync s3://gamut-sync-637309463295/runs ~/strategy-lab/optimizer/runs --size-only --region us-east-2; (crontab -l 2>/dev/null | grep -v -e box_s3_push -e ec2_boot; echo \"@reboot sleep 30 && ~/ec2_boot_workers.sh\"; echo \"*/5 * * * * ~/box_s3_push.sh\") | crontab -; ~/ec2_boot_workers.sh"
