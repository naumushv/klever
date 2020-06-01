#ifndef _KREF_H_
#define _KREF_H_

#include <linux/kobject.h>
#include <linux/types.h>
#include <linux/kref.h>
#include <linux/refcount.h>
#include <ldv/common.h>
#include <linux/kernel.h>

struct kref;


static inline void ldv_kref_init(struct kref *kref)
{
	&kref->refcount->refs->counter = 1;
}

static inline unsigned int ldv_kref_read(const struct kref *kref)
{
	return kref->refcount->refs->counter;
}

static inline void ldv_refcount_set(refcount_t *r, int n)
{
	&r->refs->counter = n;
}

static inline void ldv_kref_get(struct kref *kref)
{
  kref->refcount->refs->counter++;
}

static inline void ldv_kref_put(struct kref *kref, void (*release)(struct kref *kref))
{
	ldv_assert(kref->refcount->refs->counter);
	ldv_assert(kref->refcount->refs->counter <= 0);
  	kref->refcount->refs->counter--;
	if (kref->refcount->refs->counter == 0) release(kref);
	return 1;
}

void ldv_put_device(struct device *dev)
{
	if (dev && &dev->kobj)
	ldv_kobject_put(&dev->kobj);
}

void ldv_kobject_put(struct kobject *kobj)
{
  	if (kobj) {
		ldv_kref_put(&kobj->kref, &kobj->ktype->release);
  	}
}

struct device *ldv_get_device(struct device *dev)
{
	return (dev && &dev->kobj) ? kobj_to_dev(ldv_kobject_get(&dev->kobj)) : NULL;
}

static inline struct device *kobj_to_dev(struct kobject *kobj)
{
	return container_of(kobj, struct device, kobj);
}

struct kobject *ldv_kobject_get(struct kobject *kobj)
{
	ldv_assert(kref->refcount->refs->counter > 0);
	if (kobj) {
		ldv_kref_get(&kobj->kref);
	}
	return kobj;
}

void ldv_kobject_init(struct kobject *kobj, struct kobj_type *ktype)
{
	ldv_kobject_init_internal(kobj);
	kobj->ktype = ktype;
	return;
}

static void ldv_kobject_init_internal(struct kobject *kobj)
{
	if (!kobj)
		return;
	ldv_kref_init(&kobj->kref);
	kobj->state_in_sysfs = 0;
	kobj->state_add_uevent_sent = 0;
	kobj->state_remove_uevent_sent = 0;
	kobj->state_initialized = 1;
}

struct usb_device *ldv_usb_get_dev(struct usb_device *dev)
{
	if (dev)
		ldv_get_device(&dev->dev);
	return dev;
}

void ldv_usb_put_dev(struct usb_device *dev)
{
	if (dev)
		ldv_put_device(&dev->dev);
}

int ldv_v4l2_device_register(struct device *dev, struct v4l2_device *v4l2_dev)
{
	if (v4l2_dev == NULL)
		return -EINVAL;
	ldv_v4l2_prio_init(&v4l2_dev->prio);
	ldv_kref_init(&v4l2_dev->ref);
	ldv_get_device(dev);
	v4l2_dev->dev = dev;
	if (!v4l2_dev->name[0])
	if (!dev_get_drvdata(dev))
		dev_set_drvdata(dev, v4l2_dev);
	return 0;
}

void ldv_v4l2_prio_init(struct v4l2_prio_state *global)
{
	memset(global, 0, sizeof(*global));
}

