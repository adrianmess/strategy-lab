#!/usr/bin/env python3
"""Build launch-template + fleet JSONs for the gorig_mh12 boxes (C fwd /
D rev, 24xlarge class — the spot-vCPU quota can't fit two more 48xls next
to the running gspot fleet). Runs ON a box with the S3 role.
Usage: build_fleet_json_orig.py <AMI> <EIPALLOC_C> <EIPALLOC_D>
"""
import base64
import json
import subprocess
import sys

AMI, EIP_C, EIP_D = sys.argv[1], sys.argv[2], sys.argv[3]
SG = "sg-0b1b15deb810aa8ad"
BUCKET = "gamut-sync-637309463295"
TPL = open("/home/ubuntu/fleet_userdata_orig.sh").read()

def userdata(eip, boot, name):
    u = (TPL.replace("REPLACE_ME_EIPALLOC", eip)
            .replace("REPLACE_ME_BOOT", boot)
            .replace("REPLACE_ME_NAME", name))
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
             for t in ("c8a.24xlarge", "c7a.24xlarge", "c6a.24xlarge")
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
    "lt_c.json": lt("gamut-orig-c", EIP_C, "ec2_boot_workers_orig.sh"),
    "lt_d.json": lt("gamut-orig-d", EIP_D, "ec2_boot_workers_orig_b.sh"),
    "fleet_c.json": fleet("gamut-orig-c"),
    "fleet_d.json": fleet("gamut-orig-d"),
}
for fn, doc in files.items():
    json.dump(doc, open("/tmp/" + fn, "w"), indent=1)
    subprocess.run(["aws", "s3", "cp", "/tmp/" + fn,
                    f"s3://{BUCKET}/code/{fn}", "--region", "us-east-2"],
                   check=True, capture_output=True)
    print("uploaded", fn)
print("DONE")
