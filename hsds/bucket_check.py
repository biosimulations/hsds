##############################################################################
# Copyright by The HDF Group.                                                #
# All rights reserved.                                                       #
#                                                                            #
# This file is part of HSDS (HDF5 Scalable Data Service), Libraries and      #
# Utilities.  The full HSDS copyright notice, including                      #
# terms governing use, modification, and redistribution, is contained in     #
# the file COPYING, which can be found at the root of the source code        #
# distribution tree.  If you do not have access to this file, you may        #
# request a copy from help@hdfgroup.org.                                     #
##############################################################################
#
# Head node of hsds cluster
# 
import asyncio
import time


from aiohttp.errors import HttpProcessingError
from aiobotocore import get_session

import config
from util.timeUtil import unixTimeToUTC
from util.s3Util import  getS3Keys, getS3JSONObj, releaseClient
from util.idUtil import getCollectionForId, getS3Key
from util.chunkUtil import getDatasetId 
import hsds_logger as log

async def listKeys(app):
    """ Get all s3 keys in the bucket and create list of objkeys and domain keys """
    log.info("listKeys start")
    # Get all the keys for the bucket
    # request include_stats, so that for each key we get the ETag, LastModified, and Size values.
    s3keys = await getS3Keys(app, include_stats=True)
    log.info("got: {} keys".format(len(s3keys)))
    domains = {}
    groups = {}
    datasets = {}
    datatypes = {}
    top_level_domains = {}
    group_cnt = 0
    dset_cnt = 0
    datatype_cnt = 0
    chunk_cnt = 0
    domain_cnt = 0
    other_cnt = 0
    bytes_in_bucket = 0
    # 24693-g-ccd7e104-f86c-11e6-8f7b-0242ac110009
    for s3key in s3keys:
        item = s3keys[s3key]
        item["used"] = False   # Mixin "Used" flag of false
        bytes_in_bucket += item["Size"]
        if len(s3key) >= 44 and s3key[0:5].isalnum() and s3key[5] == '-' and s3key[6] in ('g', 'd', 'c', 't'):
            objid = s3key[6:]
            
            if objid[0] == 'g':
                groups[objid] = item
                group_cnt += 1
            elif objid[0] == 'd':
                # add a cunks dictionary that we'll use to store chunk keys later
                item["chunks"] = {}
                datasets[objid] = item
                dset_cnt += 1
            elif objid[0] == 't':
                datatypes[objid] = item
                datatype_cnt += 1
            elif objid[0] == 'c':
                chunk_cnt += 1
        elif s3key == "headnode":
            item["used"] = True   # Mark used
        elif s3key.endswith(".txt"):
            # ignore collection files
            item["used"] = True   # Mark used
        elif s3key.endswith("/.domain.json"):
            item["used"] = True   # Mark used
            n = s3key.index('/')
            if n == 0:
                log.warn("unexpected domain name (leading slash): {}".format(s3key))
            elif n == -1:
                log.warn("unexpected domain name (no slash): {}".format(s3key))
            else:
                tld = s3key[:n]
                if tld not in top_level_domains:
                    top_level_domains[tld] = {}
                domain_cnt += 1
                # TBD - add a domainUtil func for this
                domain = '/' + s3key[:-(len("/.domain.json"))]
                domains[domain] = {}
                #domains[domain] = {"groups": {}, "datasets": {}, "datatypes": {}}
            
        else:
            log.warn("unknown object: {}".format(s3key))
    log.info("domain_cnt: {}".format(domain_cnt))
    log.info("group_cnt: {}".format(group_cnt))
    log.info("dset_cnt: {}".format(dset_cnt))
    log.info("datatype_cnt: {}".format(datatype_cnt))
    log.info("chunk_cnt: {}".format(chunk_cnt))
    log.info("other_cnt: {}".format(other_cnt))
    log.info("top_level_domains:")
    for tld in top_level_domains:
        log.info(tld)    
    
    app["s3keys"] = s3keys
    app["domains"] = domains
    app["groups"] = groups
    app["datasets"] = datasets
    app["datatypes"] = datatypes
    app["bytes_in_bucket"] = bytes_in_bucket

    chunk_del = []  # list of chunk ids that no longer have a dataset
    chunk_count = 0
    # iterate through s3keys again and add any chunks to the corresponding dataset
    for s3key in s3keys:
        if len(s3key) >= 44 and s3key[0:5].isalnum() and s3key[5] == '-' and s3key[6] == 'c':
            chunk_id = s3key[6:]
            dset_id = getDatasetId(chunk_id)
            if dset_id not in datasets:
                chunk_del.append(chunk_id)
            else:
                item = s3keys[s3key]  # Dictionary of ETag, LastModified, and Size
                item["used"] = True
                dset = datasets[dset_id]
                dset_chunks = dset["chunks"]
                dset_chunks[chunk_id] = item
                chunk_count += 1

    app["chunk_count"] = chunk_count
    log.info("chunk delete list ({} items):".format(len(chunk_del)))
    for chunk_id in chunk_del:
        #log.info(chunk_id)
        pass

    log.info("listKeys done")

async def markObj(app, domain, objid=None):
    """ Mark obj as in-use and for group objs, recursively call for hardlink objects 
    """
    
    domains = app["domains"]
    if domain not in domains:
        log.error("Expected to find domain: {} in domains collection".format(domain))
        return
    domain_obj = domains[domain]
    
    # if no objid, start with the root
    if objid is None:        
        s3key = getS3Key(domain)
        obj_json = await getS3JSONObj(app, s3key)
        if "root" not in obj_json:
            # Skip folder domains
            log.info("no root for {} (domain folder)".format(domain))
            return
        # create groups, datasets, and datatypes collection
        domain_obj["groups"] = {}
        domain_obj["datasets"] = {}
        domain_obj["datatypes"] = {}
        objid = obj_json["root"]
        log.info("{} root: {}".format(domain, objid))

    log.info("markObj: {}".format(objid))
    collection = getCollectionForId(objid)
    domain_ids = domain_obj[collection]
    if objid in domain_ids:
        log.warn("already visited obj {}".format(objid))
        return
    bucket_ids = app[collection]
    if objid not in bucket_ids:
        log.warn("Expected to find {} in domain: {}".format(objid, domain))
     
    log.info("markObj: {}".format(objid))
    if objid not in bucket_ids:
        log.warn("Expected to find id: {} in bucket (s3key: {})".format(objid, getS3Key(objid)))
        return
    obj = bucket_ids[objid]
    if obj["used"]:
        # we must have already visited this object and its children before
        # i.e. through a loop in the graph, so just return here
        log.warn("Expected used state to be False for obj: {}".format(objid))
        return
    domain_ids[objid] = obj  # add to domain collection
    obj["used"] = True  # in use
    if collection == "groups":
        # add the objid to our domain list by collection type
        s3key = getS3Key(objid)
        try:
            group_json = await getS3JSONObj(app, s3key)
        except HttpProcessingError as hpe:
            log.warn("Got error retrieving key {}: {}".format(s3key, hpe))
            return
        if "domain" not in group_json:
            log.warn("Expected to find domain key for obj: {} (s3key: {})".format(objid, getS3Key(objid)))
            return
        if group_json["domain"] != domain:
            log.warn("Unexpected domain for obj {}: {}".format(objid, group_json["domain"]))
            return
        # For group objects, iteratore through all the hard lines and mark those objects
        if "links"  not in group_json:
            log.warn("Expected to find links key in groupjson for obj: {} (s3key: {})".formt(objid, getS3Key(objid)))
            return
        links = group_json["links"]
        for link_name in links:
            link_json = links[link_name]
            if "class" not in link_json:
                log.warn("Expected to find class key for link: {} in obj: {}".format(link_name, objid))
                continue
            if link_json["class"] == "H5L_TYPE_HARD":
                link_id = link_json["id"]
                await markObj(app, domain, link_id)  

async def bucketCheck(app):
    """ Verify that contents of bucket are self-consistent
    """
 
    now = int(time.time())
    log.info("bucket check {}".format(unixTimeToUTC(now)))
    # do initial listKeys
    await listKeys(app)
     
    domains = app["domains"]
    # check each domain
    for domain in domains:
        await markObj(app, domain)

    s3keys = app["s3keys"]
    unlink_count = 0
    for s3key in s3keys:
        obj = s3keys[s3key]
        if not obj["used"]:
            print("Key: {} not linked".format(s3key))
            unlink_count += 1

    print("total storage: {}".format(app["bytes_in_bucket"]))
    print("Num domains: {}".format(len(app["domains"])))
    print("Num groups: {}".format(len(app["groups"])))
    print("Num datatypes: {}".format(len(app["datatypes"])))
    print("Num datasets: {}".format(len(app["datasets"])))
    print("Num chunks: {}".format(app["chunk_count"]))
    print("Unlinked objects: {}".format(unlink_count))
 

#
# Main
#

if __name__ == '__main__':    
    # we need to setup a asyncio loop to query s3
    loop = asyncio.get_event_loop()
    app = {}
    app["bucket_name"] = config.get("bucket_name")
    app["loop"] = loop
    session = get_session(loop=loop)
    app["session"] = session
    loop.run_until_complete(bucketCheck(app))
    releaseClient(app)
    loop.close()

    print("done!")

     
