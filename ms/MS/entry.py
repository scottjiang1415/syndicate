#!/usr/bin/pyhon

"""
   Copyright 2013 The Trustees of Princeton University
   All Rights Reserved
"""


import storage.storagetypes as storagetypes

import protobufs.ms_pb2 as ms_pb2
import logging

import random
import os

import types
import errno
import time
import datetime
import collections

from volume import Volume

from msconfig import *

MSENTRY_TYPE_FILE = ms_pb2.ms_entry.MS_ENTRY_TYPE_FILE
MSENTRY_TYPE_DIR = ms_pb2.ms_entry.MS_ENTRY_TYPE_DIR

def is_readable( user_id, volume_owner_id, ent_owner_id, ent_mode ):
   return ent_owner_id == user_id or volume_owner_id == user_id or (ent_mode & 0044) != 0

def is_writable( user_id, volume_owner_id, ent_owner_id, ent_mode ):
   return ent_owner_id == user_id or volume_owner_id == user_id or (ent_mode & 0022) != 0


def latest_time( mtime_sec, mtime_nsec ):
   now_sec, now_nsec = storagetypes.clock_gettime()

   if mtime_sec < now_sec or (mtime_sec == now_sec and mtime_nsec < now_nsec):
      mtime_sec = now_sec
      mtime_nsec = now_nsec + 1
      if mtime_nsec > 1e9:
         mtime_nsec = 0
         mtime_sec += 1

   return (mtime_sec, mtime_nsec)

   
class MSEntryShard(storagetypes.Object):
   # Sharded component of an MSEntry.
   # NOTE: size doesn't change for directories, but the mtime_sec, mtime_nsec fields will.
   # They don't have to increase monotonically for directories---all the UG needs to determine is that the directory
   # has changed at all.  The concept of a "latest" shard is applicable only for files.
   mtime_sec = storagetypes.Integer(default=0, indexed=False)
   mtime_nsec = storagetypes.Integer(default=0, indexed=False)
   size = storagetypes.Integer(default=0, indexed=False )
   write_nonce = storagetypes.Integer( default=0, indexed=False )
   
   # version of the MSEntry we're associated with
   msentry_version = storagetypes.Integer(default=0, indexed=False )

   # volume ID of the MSEntry we're associated with
   msentry_volume_id = storagetypes.Integer( indexed=False )
   
   @classmethod
   def modtime_max( cls, m1, m2 ):
      # return the later of two MSEntryShards, based on moddtime
      if m1.mtime_sec < m2.mtime_sec:
         return m2

      elif m1.mtime_sec > m2.mtime_sec:
         return m1

      elif m1.mtime_nsec < m2.mtime_nsec:
         return m2

      elif m1.mtime_nsec > m2.mtime_nsec:
         return m1

      return m1

   @classmethod
   def size_max( cls, sz1, sz2 ):
      if sz1.size > sz2.size:
         return sz1

      return sz2
   
   @classmethod
   def get_latest_attr( cls, ent, shards, attr ):
      mm = MSEntryShard.get_latest_shard( ent, shards )
      if mm != None:
         return getattr( mm, attr )
      else:
         return None
   
   @classmethod
   def get_latest_shard( cls, ent, shards ):
      mm = None
      latest_version = ent.version
      latest_shard = None
      for shard in shards:
         if mm == None:
            mm = shard
            continue

         if shard == None:
            continue

         if shard.msentry_version != latest_version:
            continue
         
         mm = MSEntryShard.modtime_max( mm, shard )
      return mm

   @classmethod
   def get_mtime_from_shards( cls, ent, shards ):
      mm = MSEntryShard.get_latest_shard( ent, shards )
      if mm != None:
         return (mm.mtime_sec, mm.mtime_nsec)
      else:
         return (None, None)

   @classmethod
   def get_size_from_shards( cls, ent, shards ):
      # NOTE: size can never decrease for a given version 
      sz = None
      latest_version = ent.version
      for shard in shards:
         if sz == None:
            sz = shard
            continue

         if shard == None:
            continue

         if shard.msentry_version != latest_version:
            continue

         sz = MSEntryShard.size_max( sz, shard )

      return sz.size
   
   
class MSEntryNameHolder( storagetypes.Object ):
   """
   Exists to prove that an MSEntry record exists in a parent under a given name
   """
   volume_id = storagetypes.Integer( default=-1, indexed=False )
   parent_id = storagetypes.String( default="None", indexed=False )
   file_id = storagetypes.String( default="None", indexed=False )
   name = storagetypes.String( default="", indexed=False )
   
   @classmethod
   def make_key_name( cls, volume_id, parent_id, name ):
      return "MSEntryNameHolder: volume_id=%s,parent_id=%s,name=%s" % (volume_id, parent_id, name)
   
   @classmethod
   def create_async( cls, _volume_id, _parent_id, _file_id, _name ):
      return MSEntryNameHolder.get_or_insert_async( MSEntryNameHolder.make_key_name( _volume_id, _parent_id, _name ), volume_id=_volume_id, parent_id=_parent_id, file_id=_file_id, name=_name )
   
   
         
class MSEntry( storagetypes.Object ):
   """
   Syndicate metadata entry.
   Each entry is named by its path.
   Each entry is in its own entity group, and is named by its path.
   Each entry refers to its parent, so we can resolve a path's worth of metadata quickly.
   """

   # stuff that can't be encrypted
   ftype = storagetypes.Integer( default=-1, indexed=False )
   file_id = storagetypes.String( default="None" )
   owner_id = storagetypes.Integer( default=-1 )
   coordinator_id = storagetypes.Integer( default=1 )
   volume_id = storagetypes.Integer( default=-1 )
   mode = storagetypes.Integer( default=0 )
   is_readable_in_list = storagetypes.Boolean( default=False )    # used to simplify queries
   parent_id = storagetypes.String( default="-1" )                # file_id value of parent directory
   deleted = storagetypes.Boolean( default=False, indexed=False ) # whether or not this directory is considered to be deleted
   
   # stuff that can be encrypted
   name = storagetypes.String( default="", indexed=False )
   version = storagetypes.Integer( default=0, indexed=False )
   ctime_sec = storagetypes.Integer( default=0, indexed=False )
   ctime_nsec = storagetypes.Integer( default=0, indexed=False )
   max_read_freshness = storagetypes.Integer( default=0, indexed=False )
   max_write_freshness = storagetypes.Integer( default=0, indexed=False )

   # stuff that can be encrypted and filled in from a shard
   mtime_sec = storagetypes.Integer( default=-1, indexed=False )
   mtime_nsec = storagetypes.Integer( default=-1, indexed=False )
   size = storagetypes.Integer( default=0, indexed=False )
   write_nonce = storagetypes.Integer( default=0, indexed=False )

   # attributes that must be supplied on creation
   required_attrs = [
      "ftype",
      "file_id",
      "name",
      "version",
      "owner_id",
      "coordinator_id",
      "volume_id",
      "mtime_sec",
      "mtime_nsec",
      "ctime_sec",
      "ctime_nsec",
      "mode",
      "size",
   ]

   # attributes that uniquely identify this entry
   key_attrs = [
      "volume_id",
      "file_id",
   ]

   # required for each call
   call_attrs = [
      "volume_id",
      "file_id",
      "name",
      "parent_id",
      "coordinator_id"
   ]

   # methods for generating default values for attributes (sharded or not)
   default_values = {
      "max_read_freshness": (lambda cls, attrs: 0),
      "max_write_freshness": (lambda cls, attrs: 0),
      "file_id": (lambda cls, attrs: "None")
   }

   # publicly readable attributes, sharded or not
   read_attrs = [
      "ftype",
      "version",
      "name",
      "ctime_sec",
      "ctime_nsec",
      "mtime_sec",
      "mtime_nsec",
      "owner_id",
      "coordinator_id",
      "volume_id",
      "mode",
      "size",
      "max_read_freshness",
      "max_write_freshness",
      "write_nonce"
   ]

   # publicly writable attributes, sharded or not
   write_attrs = [
      "name",
      "version",
      "mode",
      "size",
      "mtime_sec",
      "mtime_nsec",
      "max_read_freshness",
      "max_write_freshness",
      "coordinator_id"
   ]

   # shard class
   shard_class = MSEntryShard
   
   # sharded fields
   shard_fields = [
      "mtime_sec",
      "mtime_nsec",
      "size",
      "msentry_version",
      "msentry_volume_id",
      "write_nonce"
   ]

   # functions that read a sharded value from shards for an instance of this ent
   shard_readers = {
      "mtime_sec": (lambda ent, shards: MSEntryShard.get_mtime_from_shards( ent, shards )[0]),
      "mtime_nsec": (lambda ent, shards: MSEntryShard.get_mtime_from_shards( ent, shards )[1]),
      "size": (lambda ent, shards: MSEntryShard.get_size_from_shards( ent, shards )),
      "write_nonce": (lambda ent, shards: MSEntryShard.get_latest_attr( ent, shards, "write_nonce" ))
   }

   # functions that write a sharded value, given this ent
   shard_writers = {
      "msentry_version": (lambda ent: ent.version),
      "msentry_volume_id": (lambda ent: ent.volume_id),
      "size": (lambda ent: ent.size),
      "write_nonce": (lambda ent: ent.write_nonce)
   }
   
   @classmethod 
   def unserialize_id( self, i ):
      return '{:016X}'.format(i)
   
   @classmethod
   def serialize_id( self, i_str ):
      return int(i_str, 16)
   
   def protobuf( self, pbent, **kwargs ):
      """
      Return an ms_entry instance containing this entry's data
      """

      pbent.type = kwargs.get( 'ftype', self.ftype )
      pbent.ctime_sec = kwargs.get( 'ctime_sec', self.ctime_sec )
      pbent.ctime_nsec = kwargs.get( 'ctime_nsec', self.ctime_nsec )
      pbent.mtime_sec = kwargs.get( 'mtime_sec', self.mtime_sec )
      pbent.mtime_nsec = kwargs.get( 'mtime_nsec', self.mtime_nsec )
      pbent.owner = kwargs.get( 'owner_id', self.owner_id )
      pbent.coordinator = kwargs.get( 'coordinator_id', self.coordinator_id )
      pbent.volume = kwargs.get( 'volume_id', self.volume_id )
      pbent.mode = kwargs.get( 'mode', self.mode )
      pbent.size = kwargs.get( 'size', self.size )
      pbent.version = kwargs.get( 'version', self.version )
      pbent.name = kwargs.get( 'name', self.name )
      pbent.max_read_freshness = kwargs.get( 'max_read_freshness', self.max_read_freshness )
      pbent.max_write_freshness = kwargs.get( 'max_write_freshness', self.max_write_freshness )
      pbent.write_nonce = kwargs.get( 'write_nonce', self.write_nonce )

      pbent.parent_id = MSEntry.serialize_id( kwargs.get('parent_id', '0000000000000000') )
      pbent.file_id = MSEntry.serialize_id( kwargs.get( 'file_id', self.file_id ) )
      
      
      return
      

   @classmethod
   def unprotobuf( cls, ent ):
      """
      Return an MSEntry instance from a protobuf.ms_pb2.ms_entry
      """
      file_id = MSEntry.unserialize_id( ent.file_id )
      ret = MSEntry( key=storagetypes.make_key( MSEntry, MSEntry.make_key_name( ent.volume, file_id )) )
      
      ret.ftype = ent.type
      ret.file_id = file_id
      ret.name = ent.name
      ret.ctime_sec = ent.ctime_sec
      ret.ctime_nsec = ent.ctime_nsec
      ret.mtime_sec = ent.mtime_sec
      ret.mtime_nsec = ent.mtime_nsec
      ret.owner_id = ent.owner
      ret.coordinator_id = ent.coordinator
      ret.volume_id = ent.volume
      ret.mode = ent.mode
      ret.size = ent.size
      ret.version = ent.version
      ret.max_read_freshness = ent.max_read_freshness
      ret.max_write_freshness = ent.max_write_freshness
      ret.write_nonce = ent.write_nonce

      if ent.HasField('parent_id'): # and ent.HasField('parent_name'):
         #ret.parent_key = storagetypes.make_key( MSEntry, MSEntry.make_key_name( ent.volume, ent.parent_id ) )
         ret.parent_id = MSEntry.unserialize_id( ent.parent_id )
      
      return ret


   @classmethod
   def unprotobuf_dict( cls, ent ):
      d = cls.unprotobuf( ent ).to_dict()

      # included sharded fields (which share the same name)
      for shard_field in cls.shard_fields:
         d[shard_field] = getattr(ent, shard_field, None)
      
      return d
      
   @classmethod
   def get_parent_path( cls, path ):
      """
      Get the path of the parent entry.
      """
      ppath = path.rstrip("/")
      if len(ppath) == 0:
         return "/"
      else:
         parent_dirname = os.path.dirname( ppath )
         if len(parent_dirname) == 0:
            return "/"
            
         return parent_dirname


   @classmethod
   def sanitize_fs_path( cls, fs_path ):
      # path should not end in /, unless it's the root directory
      if fs_path != '/':
         if fs_path[-1] == '/':
            return fs_path[:-1]
         else:
            return fs_path[:]

      else:
         return fs_path[:]


   @classmethod
   def make_key_name( cls, volume_id=None, file_id=None ):
      return super( MSEntry, cls ).make_key_name( volume_id=volume_id, file_id=file_id )
   
   @classmethod
   def cache_key_name( cls, volume_id=None, file_id=None ):
      return super( MSEntry, cls ).cache_key_name( volume_id=volume_id, file_id=file_id )
      
   @classmethod
   def cache_listing_key_name( cls, volume_id=None, file_id=None ):
      return super( MSEntry, cls ).cache_listing_key_name( volume_id=volume_id, file_id=file_id )


   def update_dir_shard( self, num_shards, parent_volume_id, parent_file_id, **parent_attrs ):
      """
      NOTE: This does NOT need to run in a transaction.
      For files, only one UG will ever send size updates, and they will be serialized and sanity-checked both by the UG and the MS.
      For directories, size does not ever change, and mtime only has to be different across updates.
      """
      key_name = MSEntry.make_key_name( parent_volume_id, parent_file_id )
      shard_keys = self.get_shard_keys( num_shards, key_name )
      shard_key = shard_keys[ random.randint( 0, len(shard_keys)-1 ) ]
      
      shard = shard_key.get()
      if shard == None:
         shard = self.shard_class( key=shard_key )
      
      MSEntry.populate_shard_inst( self, shard, **parent_attrs )

      shard.put_async()

      self.write_shard = shard
      
      return shard


   @classmethod
   @storagetypes.concurrent
   def __read_msentry_base( cls, volume_id, file_id, **ctx_opts ):
      ent_key = storagetypes.make_key( MSEntry, MSEntry.make_key_name( volume_id, file_id ) )
      ent = yield ent_key.get_async( **ctx_opts )
      storagetypes.concurrent_return( ent )


   @classmethod
   @storagetypes.concurrent
   def __read_msentry_shards( cls, volume_id, file_id, num_shards, **ctx_opts ):
      key_name = MSEntry.make_key_name( volume_id, file_id )
      shard_keys = MSEntry.get_shard_keys( num_shards, key_name )
      shards = yield storagetypes.get_multi_async( shard_keys, **ctx_opts )
      storagetypes.concurrent_return( shards )

   @classmethod
   @storagetypes.concurrent
   def __read_msentry( cls, volume_id, file_id, num_shards, **ctx_opts ):
      ent, shards = yield MSEntry.__read_msentry_base( volume_id, file_id, **ctx_opts ), MSEntry.__read_msentry_shards( volume_id, file_id, num_shards, **ctx_opts )
      if ent != None:
         ent.populate_from_shards( shards )

      storagetypes.concurrent_return( ent )


   @classmethod
   def check_call_attrs( cls, ent_attrs ):

      # verify that we have the appropriate attributes
      needed = []
      for key_attr in cls.call_attrs:
         if key_attr not in ent_attrs.keys():
            needed.append( key_attr )

      if len(needed) > 0:
         logging.error( "Missing update attrs: %s" % (",".join(needed) ) )
         return -errno.EINVAL;

      return 0

   @classmethod
   def preprocess_attrs( cls, ent_attrs ):
      # do some preprocessing on the ent attributes...
      if (ent_attrs['mode'] & 0444) != 0:
         ent_attrs['is_readable_in_list'] = True
      else:
         ent_attrs['is_readable_in_list'] = False
      
   
   @classmethod
   def update_parent_shard( cls, num_shards, parent_ent ):
      
      # put the parent shard, updating the number of children
      parent_attrs = {}
      parent_attrs.update( parent_ent.to_dict() )

      now_sec, now_nsec = storagetypes.clock_gettime()

      # verify that the clock advances
      if now_sec < parent_attrs['mtime_sec'] or (now_sec == parent_attrs['mtime_sec'] and now_nsec < parent_attrs['mtime_nsec']):
         now_sec = max( now_sec, parent_attrs['mtime_sec'] )
         now_nsec = max( now_nsec, parent_attrs['mtime_nsec'] ) + 1
         
         if now_nsec >= 1000000000:
            now_nsec = 0
            now_sec += 1
         
      parent_attrs['mtime_sec'] = now_sec
      parent_attrs['mtime_nsec'] = now_nsec
      parent_attrs['write_nonce'] = random.randint( -2**63, 2**63 - 1 )
      
      key_name = MSEntry.make_key_name( parent_ent.volume_id, parent_ent.file_id )
      shard_key = MSEntry.get_shard_key( key_name, random.randint( 0, num_shards-1 ) )
      
      shard = parent_ent.write_shard
      if shard == None:
         shard = MSEntry.shard_class( key=shard_key )
      
      MSEntry.populate_shard_inst( parent_ent, shard, **parent_attrs )
      
      parent_ent.write_shard = shard
      
      return shard
   
   
   @classmethod
   def update_parent_async( cls, num_shards, parent_ent ):
      shard = MSEntry.update_parent_shard( num_shards, parent_ent )

      shard_fut = shard.put_async()
      return shard_fut
      
      
      
   @classmethod
   def Create( cls, user_owner_id, volume, **ent_attrs ):
      # return the file_id on success
      # coerce volume_id
      ent_attrs['volume_id'] = volume.volume_id

      rc = MSEntry.check_call_attrs( ent_attrs )
      if rc != 0:
         return rc

      # get parent name and ID
      parent_id = ent_attrs['parent_id']

      # ensure we have every required attribute
      MSEntry.fill_defaults( ent_attrs )

      # necessary input
      missing = MSEntry.find_missing_attrs( ent_attrs )
      if len(missing) > 0:
         logging.error("missing: %s" % missing)
         return -errno.EINVAL

      # get child vitals
      child_name = ent_attrs["name"]
      child_id = ent_attrs["file_id"]
      volume_id = volume.volume_id

      # valid input
      invalid = MSEntry.validate_fields( ent_attrs )
      if len(invalid) > 0:
         logging.error("not allowed: %s" % invalid)
         return -errno.EINVAL

      # get a Child ID
      if child_name == '/':
         # are we creating root?
         child_id = 0
         ent_attrs['file_id'] = '0000000000000000'

      if child_id == 0 and user_owner_id != 0 and volume.owner_id != user_owner_id:
         # can't create root if we don't own the Volume, or aren't admin
         return -errno.EACCES
      
      # get the parent entry outside of the transaction
      parent_ent = None
      child_ent = None
      parent_fut = None
      futs = []

      parent_cache_key_name = MSEntry.cache_key_name( volume_id, parent_id )

      parent_ent = storagetypes.memcache.get( parent_cache_key_name )

      if parent_ent == None:
         parent_key = storagetypes.make_key( MSEntry, MSEntry.make_key_name( volume_id, parent_id ) )
         parent_fut = parent_key.get_async( use_memcache=False )
         futs.append( parent_fut )
      
      
      # do some preprocessing on the ent attributes...
      MSEntry.preprocess_attrs( ent_attrs )
      
      # try to add a nameholder (should create a new one )
      nameholder_fut = MSEntryNameHolder.create_async( volume_id, parent_id, child_id, ent_attrs['name'] )
      futs.append( nameholder_fut )
      
      storagetypes.wait_futures( futs )
      
      # get futures...
      if parent_ent == None:
         parent_ent = parent_fut.get_result()

      nameholder = nameholder_fut.get_result()
      
      # if parent was deleted, then roll back
      if parent_ent == None or parent_ent.deleted:
         storagetypes.deferred.defer( MSEntry.delete_all, [nameholder.key] )
         return -errno.ENOENT

      # if parent isn't writeable, then roll back
      if not is_writable( user_owner_id, volume_id, parent_ent.owner_id, parent_ent.mode ):
         storagetypes.deferred.defer( MSEntry.delete_all, [nameholder.key] )
         return -errno.EACCES

      # check for namespace collision
      if nameholder.file_id != child_id or nameholder.parent_id != parent_id or nameholder.volume_id != volume_id or nameholder.name != ent_attrs['name']:
         # nameholder already existed
         storagetypes.deferred.defer( MSEntry.delete_all, [nameholder.key] )
         return -errno.EEXIST
      
      # cache parent...
      storagetypes.memcache.add( parent_cache_key_name, parent_ent )

      # no collision.  Create the child
      child_ent = MSEntry( key=storagetypes.make_key( MSEntry, MSEntry.make_key_name( volume_id, child_id ) ) )
      child_ent.populate( volume.num_shards, **ent_attrs )
      
      parent_shard = MSEntry.update_parent_shard( volume.num_shards, parent_ent )
      
      futs = storagetypes.put_multi_async( [child_ent, child_ent.write_shard, parent_shard] )
      
      # make sure parent still exists
      parent_ent = storagetypes.memcache.get( parent_cache_key_name )
      
      delete = False 
      ret = 0
      
      if parent_ent == None:
         parent_key = storagetypes.make_key( MSEntry, MSEntry.make_key_name( volume_id, parent_id ) )
         parent_ent = parent_key.get()
      
      if parent_ent == None or parent_ent.deleted:
         # roll back...
         delete = True 
         ret = -errno.ENOENT
         
      # wait for operations to finish...
      storagetypes.wait_futures( futs )
      
      if delete:
         # roll back
         storagetypes.deferred.defer( MSEntry.delete_all, [fut.get_result() for fut in futs] )

      # invalidate caches
      storagetypes.memcache.delete_multi( [MSEntry.cache_listing_key_name( volume_id, parent_id ), MSEntry.cache_key_name( volume_id, parent_id ) ] )

      if ret == 0:
         storagetypes.deferred.defer( Volume.increase_file_count, volume_id )
         ret = MSEntry.serialize_id( child_id )

      return ret



   @classmethod
   def Update( cls, user_owner_id, volume, **ent_attrs ):

      rc = MSEntry.check_call_attrs( ent_attrs )
      if rc != 0:
         return rc
      
      # Update an MSEntry.
      # A file will be updated by at most one UG, so we don't need a transaction.
      # A directory can be updated by anyone, but the update conflict resolution is last-write-wins.

      volume_id = volume.volume_id
      file_id = ent_attrs['file_id']
      ent_name = ent_attrs['name']
      parent_id = ent_attrs['parent_id']

      # only write writable attributes
      writable_attrs = {}
      writable_attrs.update( ent_attrs )

      not_writable = MSEntry.validate_write( ent_attrs.keys() )
      for nw in not_writable:
         del writable_attrs[nw]

      # get the ent
      # try from cache first
      cache_ent_key = MSEntry.cache_key_name( volume_id, file_id )

      ent = storagetypes.memcache.get( cache_ent_key )
      if ent == None:
         # not in the cache.  Get from the datastore
         ent_fut = MSEntry.__read_msentry( volume_id, file_id, volume.num_shards, use_memcache=False )
         ent = ent_fut.get_result()

      if ent == None:
         return -errno.ENOENT

      # does this user have permission to write?
      if not is_writable( user_owner_id, volume.owner_id, ent.owner_id, ent.mode ):
         return -errno.EACCES

      put_ent = False
      
      # do some preprocessing on the ent attributes...
      MSEntry.preprocess_attrs( ent_attrs )
      
      # necessary, since the version can change (i.e. on a truncate)
      for write_attr in writable_attrs.keys():
         if write_attr in MSEntry.shard_fields:
            continue
         
         if getattr(ent, write_attr, None) != writable_attrs[write_attr]:
            put_ent = True
            ent.populate_base( **writable_attrs )
            break
         
      
      # last write...
      ent_attrs['write_nonce'] = random.randint( -2**63, 2**63 - 1 )
      
      # make a new shard with the mtime, write_nonce, and size
      ent.populate_shard( volume.num_shards, **ent_attrs )

      if put_ent:
         futs = storagetypes.put_multi_async( [ent, ent.write_shard] )
      else:
         futs = [ent.put_shard_async()]
      
      # invalidate cached items
      storagetypes.memcache.set( cache_ent_key, ent )

      storagetypes.wait_futures( futs )

      return 0



   # deferred operation
   @classmethod
   def delete_all( cls, shard_keys ):
      storagetypes.delete_multi( shard_keys )

   @classmethod
   def Delete( cls, user_owner_id, volume, **ent_attrs ):

      rc = MSEntry.check_call_attrs( ent_attrs )
      if rc != 0:
         return rc
      
      # delete an MSEntry.
      # A file will be deleted by at most one UG
      # A directy can be deleted by anyone, and it must be empty

      volume_id = volume.volume_id
      file_id = ent_attrs['file_id']
      parent_id = ent_attrs['parent_id']
      futs = []

      # get ent, parent_ent from the cache
      ent_cache_key_name = MSEntry.cache_key_name( volume_id, file_id )
      parent_cache_key_name = MSEntry.cache_key_name( volume_id, parent_id )

      ret = storagetypes.memcache.get_multi( [ent_cache_key_name, parent_cache_key_name] )

      ent = ret.get( ent_cache_key_name, None )
      parent_ent = ret.get( parent_cache_key_name, None )

      # if ent is not cached, then read from the datastore
      if ent == None:
         ent_fut = MSEntry.__read_msentry( volume_id, file_id, volume.num_shards, use_memcache=False )
         futs.append( ent_fut )

      # if parent_ent is not cached, then read from the datastore
      if parent_ent == None:
         parent_ent_fut = MSEntry.__read_msentry( volume_id, parent_id, volume.num_shards, use_memcache=False )
         futs.append( parent_ent_fut )

      # wait for the datastore to get back to us...
      if len(futs) != 0:
         storagetypes.wait_futures( futs )

      if ent == None:
         ent = ent_fut.get_result()

      if parent_ent == None:
         parent_ent = parent_ent_fut.get_result()

      # sanity check
      if ent == None or parent_ent == None:
         return -errno.ENOENT

      # permission checks...
      if not is_writable( user_owner_id, volume.owner_id, parent_ent.owner_id, parent_ent.mode ):
         return -errno.EACCES

      if not is_writable( user_owner_id, volume.owner_id, ent.owner_id, ent.mode ):
         return -errno.EACCES

      # sanity check
      if parent_ent.ftype != MSENTRY_TYPE_DIR:
         return -errno.ENOTDIR

      do_delete = False
      ret = 0
      ent_key_name = MSEntry.make_key_name( volume_id, file_id )
      ent_shard_keys = MSEntry.get_shard_keys( volume.num_shards, ent_key_name )
      ent_fut = None

      if ent.ftype == MSENTRY_TYPE_FILE:
         # this is a file; just delete it and update the parent mtime
         do_delete = True
         ret = 0

         # erase from the cache
         storagetypes.memcache.delete( ent_cache_key_name )

      else:
         
         # mark as deleted.  Creates will fail from now on
         ent.deleted = True
         ent.put()
         storagetypes.memcache.set( ent_cache_key_name, ent )
         
         # make sure the ent was actually empty
         qry_ents = MSEntry.query()
         qry_ents = qry_ents.filter( MSEntry.volume_id == volume_id, MSEntry.parent_id == file_id )
         child = qry_ents.fetch( 1, keys_only=True )

         if len(child) != 0:
            # attempt to get this (since queries are eventually consistent, but gets on an entity group are strongly consistent)
            child_ent = child[0].get()

            if child_ent != None:
               # not empty
               ent.deleted = False
               ent.put()

               # uncache
               storagetypes.memcache.delete( ent_cache_key_name )
               return -errno.ENOTEMPTY
         
         # clear from cache
         storagetypes.memcache.delete( ent_cache_key_name )
         
         # safe to delete
         do_delete = True
         ret = 0

      if do_delete:

         ent_fut = None
            
         # update the parent shard...
         parent_shard_fut = MSEntry.update_parent_async( volume.num_shards, parent_ent )

         # delete any listings of this parent
         storagetypes.memcache.delete_multi( [MSEntry.cache_listing_key_name( volume_id, parent_id), ent_cache_key_name] )

         # delete this entry, its shards, and its nameholder
         ent_key = storagetypes.make_key( MSEntry, MSEntry.make_key_name( volume_id, file_id ) )
         nh_key = storagetypes.make_key( MSEntryNameHolder, MSEntryNameHolder.make_key_name( volume_id, parent_id, ent.name ) )
         storagetypes.deferred.defer( MSEntry.delete_all, [nh_key, ent_key] + ent_shard_keys )
         
         # decrease number of files
         storagetypes.deferred.defer( Volume.decrease_file_count, volume_id )

         # wait for this to finish
         parent_shard_fut.get_result()

      return ret


   @classmethod
   @storagetypes.concurrent
   def __read_msentry_key_mapper( cls, volume_id, file_id, num_shards ):
      msentry, shards = yield MSEntry.__read_msentry_base( volume_id, file_id ), MSEntry.__read_msentry_shards( volume_id, file_id, num_shards )
      
      if msentry == None:
         storagetypes.concurrent_return( (None, None) )
      
      msentry.populate_from_shards( shards )

      cache_key = MSEntry.cache_key_name( msentry.volume_id, msentry.file_id )
      storagetypes.concurrent_return( (cache_key, msentry) )


   @classmethod
   def ListAll( cls, volume, file_id, owner_id=None, file_ids_only=False, no_check_memcache=False ):
      """
      Find all entries that are immediate children of this one.
      """
      if not isinstance( file_id, types.StringType ):
         file_id = MSEntry.unserialize_id( file_id )
      
      volume_id = volume.volume_id
      listing_cache_key = MSEntry.cache_listing_key_name( volume_id, file_id )
      
      client = storagetypes.memcache.Client()
      cas = False
      
      cas = True
      file_ids = None
      
      if not no_check_memcache:
         file_ids = storagetypes.memcache.get( listing_cache_key )
      
      if file_ids == None:
         # generate ent keys
         # put the query into disjunctive normal form...
         qry_ents = MSEntry.query()

         if owner_id != None:
            # owner given
            qry_ents = qry_ents.filter( storagetypes.opOR( storagetypes.opAND( MSEntry.volume_id == volume_id, MSEntry.parent_id == file_id, MSEntry.owner_id == owner_id, MSEntry.mode >= 0400 ),
                                                           storagetypes.opAND( MSEntry.volume_id == volume_id, MSEntry.parent_id == file_id, MSEntry.is_readable_in_list == True) ) )

         else:
            # no owner given
            qry_ents = qry_ents.filter( storagetypes.opAND( MSEntry.volume_id == volume_id, MSEntry.parent_id == file_id, MSEntry.is_readable_in_list == True) )
            
         
         ent_file_ids = qry_ents.fetch(None, projection=[MSEntry.file_id])
         
         file_ids = [ x.file_id for x in ent_file_ids ]
         
         if not no_check_memcache:
            storagetypes.memcache.set( listing_cache_key, file_ids )
    
         
      if file_ids_only:
         return file_ids
      
      # check memcache
      missing = file_ids
      ents = []
      
      if not no_check_memcache:
         cache_lookup = [None] * len(file_ids)
         for i in xrange(0,len(file_ids)):
            cache_key = MSEntry.cache_key_name( volume_id, file_ids[i] )
            cache_lookup[i] = cache_key
         
         ents_dict = storagetypes.memcache.get_multi( cache_lookup )
         
         # what's missing?
         missing = []
         for i in xrange(0,len(file_ids)):
            ent = ents_dict.get( cache_lookup[i] )
            if ent == None:
               missing.append( file_ids[i] )
            else:
               ents.append( ent )
         
      
      if len(missing) > 0:
         # fetch the rest from the datastore
         logging.info("Missing: %s" % missing)
         
         ent_tuples = [MSEntry.__read_msentry_key_mapper( volume_id, fid, volume.num_shards ) for fid in missing]
         
         ent_futs = filter( lambda x: x != None, ent_tuples )
         
         # wait for them all
         storagetypes.wait_futures( ent_futs )
         
         ent_results = filter( lambda x: x[0] != None and x[1] != None, [f.get_result() if f != None else None for f in ent_tuples] )
         
         if not no_check_memcache:
            # cache them, converting from [(string, MSEntry)] to {string: MSEntry}
            storagetypes.memcache.set_multi( dict( ent_results ) )
         
         # combine MSEntry results
         ents += [result[1] for result in ent_results]
         
      
      return ents
   

   @classmethod
   def SetCache( cls, ent ):
      ent_cache_key_name = MSEntry.cache_key_name( ent.volume_id, ent.file_id )
      storagetypes.memcache.set( ent_cache_key_name, ent )
      return 0
      

   @classmethod
   def SetCacheMulti( cls, ents ):
      ent_dict = {}
      for i in xrange(0,len(ents)):
         ent_cache_key_name = MSEntry.cache_key_name( ents[i].volume_id, ents[i].file_id )
         ent_dict[ ent_cache_key_name ] = ents[i]
         
      storagetypes.memcache.set_multi( ent_dict )
      return
      

   @classmethod
   def Read( cls, volume, file_id, memcache_keys_only=False, futs_only=False, no_check_memcache=False ):
      """
      Given an entry's key information, get the MSEntry
      """

      if not isinstance( file_id, types.StringType ):
         file_id = MSEntry.unserialize_id( file_id )
         
      ent_key_name = MSEntry.make_key_name( volume.volume_id, file_id)
      ent_cache_key_name = MSEntry.cache_key_name( volume.volume_id, file_id )

      ent_key = storagetypes.make_key( MSEntry, ent_key_name )
      shard_keys = MSEntry.get_shard_keys( volume.num_shards, ent_key_name )

      if memcache_keys_only:
         return ent_cache_key_name
         
      ent = None
      shard = None

      if not futs_only:
         # check cache?
         if not no_check_memcache:
            ent = storagetypes.memcache.get( ent_cache_key_name )

      # get the values from the datastore, if they weren't cached 
      futs = {}
      all_futs = []

      if ent == None or futs_only:
         futs["base"] = ent_key.get_async()
         all_futs.append( futs["base"] )

         futs["shard"] = [None] * len(shard_keys)
         for i in xrange(0, len(shard_keys)):
            shard_key = shard_keys[i]
            fut = shard_key.get_async()
            futs["shard"][i] = fut
            all_futs.append( fut )

      if futs_only:
         # only want futures, but if we have the entries, then just return them
         MSEntryFutures = collections.namedtuple( 'MSEntryFutures', ['base_future', 'shard_futures'] )
         return MSEntryFutures( base_future=futs["base"], shard_futures=futs["shard"] )

      # get the values
      if len(all_futs) > 0:
         storagetypes.wait_futures( all_futs )

         if futs.get("base", None) != None:
            ent = futs["base"].get_result()

         if ent != None and futs.get("shard", None) != None:
            shards = filter( lambda x: x != None, [x.get_result() for x in futs["shard"]] )
            ent.populate_from_shards( shards )

      # cache result
      if not no_check_memcache:
         storagetypes.memcache.set( ent_cache_key_name, ent )

      return ent


   @classmethod
   def FlattenFuture( cls, ent_fut ):
      
      all_futures = [None] * (len(ent_fut.shard_futures) + 1)
      for i in xrange(0, len(ent_fut.shard_futures)):
         all_futures[i] = ent_fut.shard_futures[i]
      
      all_futures[len(all_futures)-1] = ent_fut.base_future
      
      return all_futures
   
   @classmethod
   def FromFuture( cls, ent_fut ):
      ent = ent_fut.base_future.get_result()
      if ent != None:
         shards = filter( lambda x: x != None, [x.get_result() for x in ent_fut.shard_futures] )
         ent.populate_from_shards( shards )
         
         #logging.info("FromFuture: %s\nshards = %s" % (ent, shards))
      
      return ent

   @classmethod
   def WaitFutures( cls, futures ):
      # futures is a list of MSEntryFutures namedtuple, from Read()
      
      all_futures = []
      for fut in futures:
         all_futures += cls.FlattenFuture( fut )
         
      storagetypes.wait_futures( all_futures )

      ret = []
      
      for fut in futures:
         ent = cls.FromFuture( fut )
         ret.append( ent )

      return ret
