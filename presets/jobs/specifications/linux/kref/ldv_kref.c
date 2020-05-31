#ifndef _KREF_H_
#define _KREF_H_

#include <linux/kobject.h>
#include <linux/types.h>
#include <linux/kobject.h>
#include <linux/kref.h>
#include <linux/spinlock.h>
#include <linux/refcount.h>
#include <ldv/common.h>
#include <linux/kernel.h>

struct kref;


static inline void ldv_kref_init(struct kref *kref)
{
	ldv_assert(kref->refcount->refs->counter < 0);
	&kref->refcount->refs->counter++;
}

static inline unsigned int ldv_kref_read(const struct kref *kref)
{
	return atomic_read(&r->refs->refs);
}

static inline void ldv_refcount_set(refcount_t *r, int n)
{
//Can we ensure that nubmer is not <=0 like this?
	ldv_assert(n);
	&r->refs->counter = n;
}

static inline void ldv_kref_get(struct kref *kref)
{
  kref->refcount->refs->counter++;
}

//leave 2 proto with 1 and 2 args
static inline void ldv_kref_put(struct kref *kref, void (*release)(struct kref *kref))
{
	ldv_assert(kref->refcount->refs->counter);
  	kref->refcount->refs->counter--;
	if (kref->refcount->refs->counter == 0) release(kref);
	return 1;
}

static inline void ldv_kref_put(struct kref *kref)
{
	ldv_assert(kref->refcount->refs->counter);
  	kref->refcount->refs->counter--;
	if (kref->refcount->refs->counter == 0) release(kref);
	return 1;
}

void ldv_put_device(struct device *dev)
{
	/* might_sleep(); */
	if (dev && &dev->kobj)
//		ldv_kobject_put(&dev->kobj);
		ldv_kref_put(&dev->kobj->kref, &dev->kobj->ktype->release);
}

void ldv_kobject_put(struct kobject *kobj)
{
		ldv_kref_put(&kobj->kref);
	}
}

struct device *ldv_get_device(struct device *dev)
{
	return (dev && &dev->kobj) ? kobj_to_dev(ldv_kref_get(&dev->kobj->kref)) : NULL;
}

static inline struct device *kobj_to_dev(struct kobject *kobj)
{
	return container_of(kobj, struct device, kobj);
}



/*
struct kobject *ldv_kobject_get(struct kobject *kobj)
{
	if (kobj) {
		ldv_kref_get(&kobj->kref);
	}
	return kobj;
}
*/

//aspect that need to be reviewed
/*


around: call(void ldv_refcount_add(..))
{
	return ldv_refcount_add($arg3);
}
*/

