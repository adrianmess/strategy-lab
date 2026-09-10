#!/usr/bin/env python3
"""Build launch-template + EC2 Fleet JSONs for the gspot_newpairs campaign
and upload them to the S3 code/ prefix. Runs ON the bootstrap box (has the
repo + S3 role). CloudShell then just: aws s3 cp ... && aws ec2 create-*.

Two independent maintain-fleets (A forward, B reverse) — August's box B was
a plain persistent spot request, but 48xlarge capacity is tight this week
and a 9-pool capacity-optimized fleet hops pools automatically. Each box's
autoteardown deletes its own fleet when the plan completes.
"""
import base64
import json
import subprocess
import sys

AMI = sys.argv[1]
EIP_A = "eipalloc-0ce13b96f05ba020a"
EIP_B = "eipalloc-01a9fc9764a7a9d25"
SG = "sg-0b1b15deb810aa8ad"
BUCKET = "gamut-sync-637309463295"
TPL = open("/home/ubuntu/strategy-lab/scripts/fleet_userdata_spotnp.sh").read()

def userdata(eip, boot, name):
    u = TPL.replace("REPLACE_ME_EIPALLOC_A", eip)
    u = u.replace("gamut-spotnp-a", name)
    u = u.replace("ec2_boot_workers_spotnp.sh", boot)
    return base64.b64encode(u.encode()).decode()

def lt(name, eip, boot):
    return dict(
        LaunchTemplateName=name,
        LaunchTemplateData=dict(
            ImageId=AMI, KeyName="gamut-key",
            SecurityGroupIds=[SG],
            IamInstanceProfile=dict(Name="gamut-box"),
            UserData=userdata(eip, boot, name),
            BlockDeviceMappings=[dict(DeviceName="/dev/sda1",
                Ebs=dict(VolumeSize=200, VolumeType="gp3",
                         DeleteOnTermination=True))],
            TagSpecifications=[dict(ResourceType="instance",
                Tags=[dict(Key="Name", Value=name)])]))

def fleet(ltname):
    pools = [dict(InstanceType=t, AvailabilityZone=z)
             for t in ("c8a.48xlarge", "c7a.48xlarge", "c6a.48xlarge")
             for z in ("us-east-2a", "us-east-2b", "us-east-2c")]
    return dict(
        SpotOptions=dict(AllocationStrategy="capacity-optimized"),
        LaunchTemplateConfigs=[dict(
            LaunchTemplateSpecification=dict(LaunchTemplateName=ltname,
                                             Version="$Latest"),
            Overrides=pools)],
        TargetCapacitySpecification=dict(TotalTargetCapacity=1,
                                         DefaultTargetCapacityType="spot"),
        Type="maintain",
        TagSpecifications=[dict(ResourceType="fleet",
            Tags=[dict(Key="Name", Value=ltname)])])

files = {
    "lt_a.json": lt("gamut-spotnp-a", EIP_A, "ec2_boot_workers_spotnp.sh"),
    "lt_b.json": lt("gamut-spotnp-b", EIP_B, "ec2_boot_workers_spotnp_b.sh"),
    "fleet_a.json": fleet("gamut-spotnp-a"),
    "fleet_b.json": fleet("gamut-spotnp-b"),
}
for fn, doc in files.items():
    json.dump(doc, open("/tmp/" + fn, "w"), indent=1)
    subprocess.run(["aws", "s3", "cp", "/tmp/" + fn,
                    f"s3://{BUCKET}/code/{fn}", "--region", "us-east-2"],
                   check=True, capture_output=True)
    print("uploaded", fn)
print("DONE")
